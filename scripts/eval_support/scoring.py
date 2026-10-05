from collections import Counter

from eval_support.schema import Review, digest


def ratio(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def equivalent_argument(actual, expected):
    if isinstance(actual, str) and isinstance(expected, str):
        aliases = {"邮箱": "email", "邮件": "email", "mail": "email", "github enterprise": "github"}
        return aliases.get(actual.strip().casefold(), actual.strip().casefold()) == aliases.get(
            expected.strip().casefold(), expected.strip().casefold()
        )
    return actual == expected


def structural(case, record):
    failures = []
    metrics = Counter()
    observed = record.get("turns", [])
    if record.get("status") != "completed" or len(observed) != len(case.turns):
        failures.append(
            {
                "stage": record.get("failure_stage", "execution_control"),
                "reason": record.get("status", "not_run"),
            }
        )
    for index, expected in enumerate(case.turns):
        if index >= len(observed):
            continue
        actual = observed[index]
        if actual.get("http_status") != expected.expected_http:
            failures.append({"stage": "input_identity", "turn": index, "reason": "http_status"})
        if actual.get("sse_error"):
            failures.append({"stage": "execution_control", "turn": index, "reason": "sse_error"})
        if expected.allowed_intents:
            metrics["intent_total"] += 1
            if actual.get("state", {}).get("intent") in expected.allowed_intents:
                metrics["intent_correct"] += 1
            else:
                failures.append({"stage": "router", "turn": index, "reason": "intent"})
        entities = actual.get("state", {}).get("entities", {})
        for name, value in expected.expected_entities.items():
            if entities.get(name) != value:
                failures.append({"stage": "entities_clarification", "turn": index, "reason": name})
        tools = actual.get("tools", [])
        for rule in expected.required_tools:
            metrics["required_tool_total"] += 1
            matching = [
                t
                for t in tools
                if t["name"] == rule.name
                and all(
                    equivalent_argument(t.get("args", {}).get(k), v)
                    for k, v in rule.arguments.items()
                )
                and t.get("result")
                and not t.get("validation_error")
            ]
            if matching:
                metrics["required_tool_completed"] += 1
            else:
                failures.append({"stage": "tool_selection", "turn": index, "reason": rule.name})
        seen = set()
        for tool in tools:
            metrics["actual_tool_total"] += 1
            signature = digest({"name": tool["name"], "args": tool.get("args", {})})
            legal = tool["name"] in expected.allowed_tools
            metrics["actual_tool_legal"] += int(legal)
            if not legal or signature in seen:
                metrics["extra_tool_calls"] += 1
                failures.append(
                    {"stage": "tool_selection", "turn": index, "reason": "unexpected_or_duplicate"}
                )
            seen.add(signature)
            execution = tool.get("result", {})
            metrics["tool_parameters_valid"] += int(not tool.get("validation_error", False))
            if execution.get("status") == "error":
                metrics["tool_execution_errors"] += 1
        response = actual.get("response", {})
        if (
            expected.expected_kind
            and response.get("custom_data", {}).get("kind") != expected.expected_kind
        ):
            failures.append({"stage": "approval_storage", "turn": index, "reason": "response_kind"})
        if (
            expected.expected_ticket_count is not None
            and actual.get("ticket_count") != expected.expected_ticket_count
        ):
            failures.append({"stage": "approval_storage", "turn": index, "reason": "ticket_count"})
    if record.get("final_ticket_count") != case.final_ticket_count:
        failures.append({"stage": "approval_storage", "reason": "final_ticket_count"})
    if (
        case.final_draft_version is not None
        and record.get("final_draft_version") != case.final_draft_version
    ):
        failures.append({"stage": "approval_storage", "reason": "draft_version"})
    return {"passed": not failures, "failures": failures, "metrics": dict(metrics)}


def summarize(cases, records, reviews):
    selected = {c.case_id for c in cases}
    records = [r for r in records if r["case_id"] in selected]
    first_attempts = {}
    for record in sorted(records, key=lambda r: r.get("attempt", 1)):
        key = (record["run_id"], record["case_id"])
        if key in first_attempts and record.get("attempt", 1) == first_attempts[key].get(
            "attempt", 1
        ):
            raise ValueError("Duplicate raw task attempt")
        first_attempts.setdefault(key, record)
    if len({cid for _, cid in first_attempts}) != len(first_attempts):
        raise ValueError("Repeated experiments cannot inflate task denominators")
    review_map = {(r.run_id, r.case_id, r.attempt): r for r in reviews}
    counts, category, tasks = Counter(), {}, []
    for case in cases:
        record = next((r for (_, cid), r in first_attempts.items() if cid == case.case_id), None)
        bucket = category.setdefault(case.category, Counter())
        bucket["planned"] += 1
        counts["planned"] += 1
        if record is None:
            counts["not_run"] += 1
            bucket["not_run"] += 1
            tasks.append({"case_id": case.case_id, "outcome": "not_run"})
            continue
        result = structural(case, record)
        counts.update(result["metrics"])
        counts["structural_pass"] += int(result["passed"])
        review = review_map.get((record["run_id"], case.case_id, record.get("attempt", 1)))
        if review and review.record_sha256 != digest(record):
            raise ValueError("Review is not bound to this raw record")
        reviewed = review is not None and review.status == "reviewed"
        if reviewed:
            counts["reviewed"] += int(case.review_required)
            point_ids = {p.point_id for p in case.points}
            if set(review.points) != point_ids:
                raise ValueError("Every expected point needs an explicit judgment")
            for claim in review.claims:
                if not 0 <= claim.turn < len(record["turns"]) or claim.quote not in str(
                    record["turns"][claim.turn].get("response", {}).get("content", "")
                ):
                    raise ValueError("Review excerpt is not present in the actual answer")
            counts["points_total"] += len(point_ids)
            counts["points_supported"] += sum(v == "yes" for v in review.points.values())
            if point_ids:
                counts["point_task_total"] += 1
                counts["point_task_sum"] += sum(v == "yes" for v in review.points.values()) / len(
                    point_ids
                )
            if case.answerability in {"answerable", "partial"}:
                counts["answerable_reviewed"] += 1
                counts["evidence_complete"] += int(review.evidence_complete == "yes")
                counts["incorrect_full_refusal"] += int(review.refusal == "full")
            if case.answerability == "unanswerable":
                counts["unanswerable_reviewed"] += 1
                counts["unanswerable_invented"] += int(
                    any(c.supported == "no" for c in review.claims)
                )
            factual = [c for c in review.claims if c.supported in {"yes", "no"}]
            if factual and case.answerability != "not_applicable":
                counts["factual_answers"] += 1
                counts["answers_with_unsupported_claims"] += int(
                    any(c.supported == "no" for c in factual)
                )
            for claim in factual:
                if claim.citation_numbers:
                    counts["cited_claims"] += 1
                    counts["supported_cited_claims"] += int(claim.supported == "yes")
            if review.missing_citations is not None:
                counts["missing_citations"] += review.missing_citations
        visible = any(
            str(t.get("response", {}).get("content", "")).strip() for t in record.get("turns", [])
        )
        semantic_pass = bool(
            reviewed
            and review.semantic_success == "yes"
            and all(v == "yes" for v in review.points.values())
            and not any(c.supported in {"no", "uncertain"} for c in review.claims)
            and (case.answerability not in {"answerable", "partial"} or review.refusal != "full")
            and review.missing_citations in {None, 0}
            and visible
        )
        confirmed = result["passed"] and (not case.review_required or semantic_pass)
        if confirmed:
            outcome = "confirmed_success"
        elif not result["passed"]:
            outcome = (
                record.get("status")
                if record.get("status") != "completed"
                else "structural_failure"
            )
        elif reviewed and (
            review.semantic_success == "no"
            or not visible
            or any(v == "no" for v in review.points.values())
            or any(c.supported == "no" for c in review.claims)
            or (case.answerability in {"answerable", "partial"} and review.refusal == "full")
            or (review.missing_citations or 0) > 0
        ):
            outcome = "semantic_failure"
        else:
            outcome = "pending_review"
        counts[outcome] += 1
        bucket[outcome] += 1
        tasks.append(
            {
                "case_id": case.case_id,
                "outcome": outcome,
                "structural": result,
                "primary_cause": review.primary_cause if reviewed else None,
            }
        )
    latencies = sorted(r["seconds"] for r in first_attempts.values() if "seconds" in r)
    return {
        "task_count": len(cases),
        "attempt_count": len(records),
        "logical_requests": sum(len(r.get("turns", [])) for r in records),
        "counts": dict(counts),
        "categories": {k: dict(v) for k, v in category.items()},
        "intent_accuracy": ratio(counts["intent_correct"], counts["intent_total"]),
        "required_tool_completion": ratio(
            counts["required_tool_completed"], counts["required_tool_total"]
        ),
        "actual_tool_legality": ratio(counts["actual_tool_legal"], counts["actual_tool_total"]),
        "tool_parameter_validity": ratio(
            counts["tool_parameters_valid"], counts["actual_tool_total"]
        ),
        "extra_tool_calls": counts["extra_tool_calls"],
        "point_coverage_micro": ratio(counts["points_supported"], counts["points_total"]),
        "point_coverage_macro": ratio(counts["point_task_sum"], counts["point_task_total"]),
        "unsupported_answer_rate": ratio(
            counts["answers_with_unsupported_claims"], counts["factual_answers"]
        ),
        "evidence_complete_rate": ratio(counts["evidence_complete"], counts["answerable_reviewed"]),
        "incorrect_refusal_rate": ratio(
            counts["incorrect_full_refusal"], counts["answerable_reviewed"]
        ),
        "unanswerable_invention_rate": ratio(
            counts["unanswerable_invented"], counts["unanswerable_reviewed"]
        ),
        "citation_support": ratio(counts["supported_cited_claims"], counts["cited_claims"]),
        "confirmed_task_success_lower_bound": ratio(counts["confirmed_success"], len(cases)),
        "review_coverage": ratio(counts["reviewed"], sum(c.review_required for c in cases)),
        "tasks": tasks,
        "quantile_method": "floor((n-1)*p)",
        "task_latency_seconds": {
            "count": len(latencies),
            "p50": latencies[int((len(latencies) - 1) * 0.5)] if latencies else None,
            "p95": latencies[int((len(latencies) - 1) * 0.95)] if latencies else None,
        },
        "review_independence": "developer assistant participated in design, implementation and review; independent human review not performed",
    }


def export_reviews(records):
    return [
        Review(
            run_id=r["run_id"],
            case_id=r["case_id"],
            attempt=r.get("attempt", 1),
            record_sha256=digest(r),
        ).model_dump()
        for r in records
    ]


def costs(events):
    unique = {}
    for event in events:
        key = event["call_id"]
        if key in unique and unique[key] != event:
            raise ValueError("Conflicting billing event")
        unique[key] = event
    buckets = {}
    for event in unique.values():
        bucket = buckets.setdefault(
            event["provider_kind"], {"attempts": 0, "known_cny": 0.0, "unknown_usage": 0}
        )
        bucket["attempts"] += 1
        if event.get("estimated_cny") is None:
            bucket["unknown_usage"] += 1
        else:
            bucket["known_cny"] += event["estimated_cny"]
    return buckets
