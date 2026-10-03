import inspect
import json
import logging
import warnings
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langchain_core._api import LangChainBetaWarning
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langfuse import Langfuse  # type: ignore[import-untyped]
from langfuse.langchain import (
    CallbackHandler,  # type: ignore[import-untyped]
)
from langgraph.types import Command, Interrupt
from langsmith import Client as LangsmithClient
from langsmith import uuid7

from agents import DEFAULT_AGENT, AgentGraph, get_agent, get_all_agent_info, load_agent
from core import settings
from execution.middleware import SupportExecutionMiddleware
from execution.redis_runtime import initialize_redis
from execution.streaming import SupportStreamingResponse
from execution.telemetry import ControlError, current
from memory import initialize_database, initialize_store
from schema import (
    ChatHistory,
    ChatHistoryInput,
    ChatMessage,
    Feedback,
    FeedbackResponse,
    ServiceMetadata,
    StreamInput,
    UserInput,
    UserThreads,
    UserThreadsInput,
)
from service.agui import router as agui_router
from service.support import execution_lock, interrupt_message, pending_payload, support_input
from service.threads import list_user_threads
from service.utils import (
    convert_message_content_to_string,
    ensure_model_available,
    langchain_to_chat_message,
    messages_from_checkpoint,
    remove_tool_calls,
)
from support_storage.identity import guard_thread, identity
from support_storage.preferences import (
    Preferences,
    delete_preferences,
    read_preferences,
    save_preferences,
)
from support_storage.runtime import initialize_support_storage
from support_storage.runtime import repository as support_repository

warnings.filterwarnings("ignore", category=LangChainBetaWarning)
logger = logging.getLogger(__name__)


def custom_generate_unique_id(route: APIRoute) -> str:
    """Generate idiomatic operation IDs for OpenAPI client generation."""
    return route.name


def verify_bearer(
    http_auth: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(HTTPBearer(description="Please provide AUTH_SECRET api key.", auto_error=False)),
    ],
) -> None:
    if not settings.AUTH_SECRET:
        return
    auth_secret = settings.AUTH_SECRET.get_secret_value()
    if not http_auth or http_auth.credentials != auth_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Configurable lifespan that initializes the appropriate database checkpointer, store,
    and agents with async loading - for example for starting up MCP clients.
    """
    try:
        # Initialize both checkpointer (for short-term memory) and store (for long-term memory)
        async with (
            initialize_database() as saver,
            initialize_store() as store,
            initialize_support_storage(),
            initialize_redis(),
        ):
            # Set up both components
            if hasattr(saver, "setup"):  # ignore: union-attr
                await saver.setup()
            # Only setup store for Postgres as InMemoryStore doesn't need setup
            if hasattr(store, "setup"):  # ignore: union-attr
                await store.setup()

            if not settings.AUTH_SECRET:
                logger.warning(
                    "AUTH_SECRET is not configured — all API endpoints are unauthenticated. "
                    "Set AUTH_SECRET in your environment to enable bearer token authentication."
                )

            # Configure agents with both memory components and async loading
            agents = get_all_agent_info()
            for a in agents:
                try:
                    await load_agent(a.key)
                    logger.info(f"Agent loaded: {a.key}")
                except Exception as e:
                    logger.error("Failed to load agent %s: %s", a.key, type(e).__name__)
                    # Continue with other agents rather than failing startup

                agent = get_agent(a.key)
                # Set checkpointer for thread-scoped memory (conversation history)
                agent.checkpointer = saver
                # Set store for long-term memory (cross-conversation knowledge)
                agent.store = store
            try:
                yield
            finally:
                from rag.retriever import close_retriever

                await close_retriever()
    except Exception as e:
        logger.error("Database/store/agents initialization failed: %s", type(e).__name__)
        raise RuntimeError(
            "Database initialization failed; verify connection and business migrations"
        ) from None


app = FastAPI(lifespan=lifespan, generate_unique_id_function=custom_generate_unique_id)
app.add_middleware(SupportExecutionMiddleware)
router = APIRouter(dependencies=[Depends(verify_bearer)])
# AG-UI protocol endpoints inherit the same bearer auth - see service/agui.py
router.include_router(agui_router)


@router.get("/info")
async def info() -> ServiceMetadata:
    models = list(settings.AVAILABLE_MODELS)
    models.sort()
    return ServiceMetadata(
        agents=get_all_agent_info(),
        models=models,
        default_agent=DEFAULT_AGENT,
        default_model=settings.DEFAULT_MODEL,
    )


async def _handle_input(
    user_input: UserInput, agent: AgentGraph, agent_id: str
) -> tuple[dict[str, Any], UUID]:
    """
    Parse user input and handle any required interrupt resumption.
    Returns kwargs for agent invocation and the run_id.
    """
    run_id = uuid7()
    if trace := current.get():
        trace.run_id = str(run_id)
    thread_id = user_input.thread_id or str(uuid4())
    user_id = (
        identity(user_input.user_id)
        if agent_id == "support-agent"
        else user_input.user_id or str(uuid4())
    )
    await guard_thread(
        thread_id,
        user_id,
        agent_id,
        create=agent_id == "support-agent",
        title=user_input.message or "",
        checkpointer=getattr(agent, "checkpointer", None),
    )

    configurable = {"thread_id": thread_id, "user_id": user_id}
    if user_input.model is not None:
        ensure_model_available(user_input.model)
        configurable["model"] = user_input.model

    callbacks: list[Any] = []
    if settings.LANGFUSE_TRACING:
        # Initialize Langfuse CallbackHandler for Langchain (tracing)
        langfuse_handler = CallbackHandler()

        callbacks.append(langfuse_handler)

    if user_input.agent_config:
        if agent_id == "support-agent":
            raise HTTPException(422, detail="Support Agent does not accept agent_config overrides")
        # Check for reserved keys (including 'model' even if not in configurable)
        reserved_keys = {"thread_id", "user_id", "model"}
        if agent_id == "support-agent":
            reserved_keys.update(
                key
                for key in user_input.agent_config
                if key.startswith("__") or key.startswith("checkpoint")
            )
        if overlap := reserved_keys & user_input.agent_config.keys():
            raise HTTPException(
                status_code=422,
                detail=f"agent_config contains reserved keys: {overlap}",
            )
        configurable.update(user_input.agent_config)

    config = RunnableConfig(
        configurable=configurable,
        metadata={"user_id": user_id, "agent_id": agent_id},
        run_id=run_id,
        callbacks=callbacks,
    )

    # Check for interrupts that need to be resumed
    state = await agent.aget_state(config=config)

    if agent_id == "support-agent":
        return {**await support_input(user_input, state, config), "config": config}, run_id
    if user_input.approval is not None:
        raise HTTPException(
            status_code=422, detail="Structured approval is only supported by support-agent"
        )

    interrupted_tasks = [
        task for task in state.tasks if hasattr(task, "interrupts") and task.interrupts
    ]

    input: Command | dict[str, Any]
    if interrupted_tasks:
        # assume user input is response to resume agent execution from interrupt
        input = Command(resume=user_input.message)
    else:
        input = {"messages": [HumanMessage(content=user_input.message or "")]}

    kwargs = {
        "input": input,
        "config": config,
    }

    return kwargs, run_id


@router.post("/{agent_id}/invoke", operation_id="invoke_with_agent_id")
@router.post("/invoke")
async def invoke(user_input: UserInput, agent_id: str = DEFAULT_AGENT) -> ChatMessage:
    """
    Invoke an agent with user input to retrieve a final response.

    If agent_id is not provided, the default agent will be used.
    Use thread_id to persist and continue a multi-turn conversation. run_id kwarg
    is also attached to messages for recording feedback.
    Use user_id to persist and continue a conversation across multiple threads.
    """
    async with execution_lock(agent_id, user_input.thread_id):
        return await _invoke(user_input, agent_id)


async def _invoke(user_input: UserInput, agent_id: str) -> ChatMessage:
    # NOTE: Currently this only returns the last message or interrupt.
    # In the case of an agent outputting multiple AIMessages (such as the background step
    # in interrupt-agent, or a tool step in research-assistant), it's omitted. Arguably,
    # you'd want to include it. You could update the API to return a list of ChatMessages
    # in that case.
    agent: AgentGraph = get_agent(agent_id)
    kwargs, run_id = await _handle_input(user_input, agent, agent_id)

    if early := kwargs.pop("support_response", None):
        output = langchain_to_chat_message(early)
        output.run_id = str(run_id)
        return output

    try:
        response_events: list[tuple[str, Any]] = await agent.ainvoke(**kwargs, stream_mode=["updates", "values"])  # type: ignore # fmt: skip
        response_type, response = response_events[-1]
        # A run that stops on an interrupt reports it on the final event of either stream
        # mode, so check for the interrupt before falling back to the last message.
        if "__interrupt__" in response:
            # Return the value of the first interrupt as an AIMessage
            output = langchain_to_chat_message(
                interrupt_message(response["__interrupt__"][0].value)
            )
        elif response_type == "values":
            # Normal response, the agent completed successfully
            output = langchain_to_chat_message(response["messages"][-1])
        else:
            raise ValueError(f"Unexpected response type: {response_type}")

        output.run_id = str(run_id)
        return output
    except ControlError:
        raise
    except Exception as e:
        logger.error("Agent execution failed: %s", type(e).__name__)
        raise HTTPException(status_code=500, detail="Unexpected error")


async def message_generator(
    user_input: StreamInput, agent_id: str = DEFAULT_AGENT
) -> AsyncGenerator[str, None]:
    async with execution_lock(agent_id, user_input.thread_id):
        generator = _message_generator(user_input, agent_id)
        try:
            async for event in generator:
                yield event
        except HTTPException as exc:
            yield f"data: {json.dumps({'type': 'error', 'content': exc.detail})}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            await generator.aclose()


async def _message_generator(
    user_input: StreamInput, agent_id: str = DEFAULT_AGENT
) -> AsyncGenerator[str, None]:
    """
    Generate a stream of messages from the agent.

    This is the workhorse method for the /stream endpoint.
    """
    agent: AgentGraph = get_agent(agent_id)
    kwargs, run_id = await _handle_input(user_input, agent, agent_id)

    if early := kwargs.pop("support_response", None):
        output = langchain_to_chat_message(early)
        output.run_id = str(run_id)
        yield f"data: {json.dumps({'type': 'message', 'content': output.model_dump()})}\n\n"
        yield "data: [DONE]\n\n"
        return

    graph_stream = agent.astream(  # type: ignore[no-matching-overload]
        **kwargs, stream_mode=["updates", "messages", "custom"], subgraphs=True
    )
    try:
        async for stream_event in graph_stream:
            if not isinstance(stream_event, tuple):
                continue
            # Handle different stream event structures based on subgraphs
            if len(stream_event) == 3:
                # With subgraphs=True: (node_path, stream_mode, event)
                _, stream_mode, event = stream_event
            else:
                # Without subgraphs: (stream_mode, event)
                stream_mode, event = stream_event
            new_messages: list[Any] = []
            if stream_mode == "updates":
                for node, updates in event.items():
                    # A simple approach to handle agent interrupts.
                    # In a more sophisticated implementation, we could add
                    # some structured ChatMessage type to return the interrupt value.
                    if node == "__interrupt__":
                        interrupt: Interrupt
                        for interrupt in updates:
                            new_messages.append(interrupt_message(interrupt.value))
                        continue
                    updates = updates or {}
                    update_messages = updates.get("messages", [])
                    # special cases for using langgraph-supervisor library
                    if "supervisor" in node or "sub-agent" in node:
                        # the only tools that come from the actual agent are the handoff and handback tools
                        if isinstance(update_messages[-1], ToolMessage):
                            if "sub-agent" in node and len(update_messages) > 1:
                                # If this is a sub-agent, we want to keep the last 2 messages - the handback tool, and it's result
                                update_messages = update_messages[-2:]
                            else:
                                # If this is a supervisor, we want to keep the last message only - the handoff result. The tool comes from the 'agent' node.
                                update_messages = [update_messages[-1]]
                        else:
                            update_messages = []
                    new_messages.extend(update_messages)

            if stream_mode == "custom":
                new_messages = [event]

            # LangGraph streaming may emit tuples: (field_name, field_value)
            # e.g. ('content', <str>), ('tool_calls', [ToolCall,...]), ('additional_kwargs', {...}), etc.
            # We accumulate only supported fields into `parts` and skip unsupported metadata.
            # More info at: https://langchain-ai.github.io/langgraph/cloud/how-tos/stream_messages/
            processed_messages = []
            current_message: dict[str, Any] = {}
            for message in new_messages:
                if isinstance(message, tuple):
                    key, value = message
                    # Store parts in temporary dict
                    current_message[key] = value
                else:
                    # Add complete message if we have one in progress
                    if current_message:
                        processed_messages.append(_create_ai_message(current_message))
                        current_message = {}
                    processed_messages.append(message)

            # Add any remaining message parts
            if current_message:
                processed_messages.append(_create_ai_message(current_message))

            for message in processed_messages:
                try:
                    chat_message = langchain_to_chat_message(message)
                    chat_message.run_id = str(run_id)
                except Exception as e:
                    logger.error(f"Error parsing message: {e}")
                    yield f"data: {json.dumps({'type': 'error', 'content': 'Unexpected error'})}\n\n"
                    continue
                # LangGraph re-sends the input message, which feels weird, so drop it
                if chat_message.type == "human" and chat_message.content == user_input.message:
                    continue
                yield f"data: {json.dumps({'type': 'message', 'content': chat_message.model_dump()})}\n\n"

            if stream_mode == "messages":
                if not user_input.stream_tokens:
                    continue
                msg, metadata = event
                if "skip_stream" in metadata.get("tags", []):
                    continue
                # For some reason, astream("messages") causes non-LLM nodes to send extra messages.
                # Drop them.
                if not isinstance(msg, AIMessageChunk):
                    continue
                content = remove_tool_calls(msg.content)
                if content:
                    # Empty content in the context of OpenAI usually means
                    # that the model is asking for a tool to be invoked.
                    # So we only print non-empty content.
                    yield f"data: {json.dumps({'type': 'token', 'content': convert_message_content_to_string(content)})}\n\n"
    except ControlError:
        raise
    except Exception as e:
        logger.error("Error in message generator: %s", type(e).__name__)
        if trace := current.get():
            trace.outcome = "sse_error"
        yield f"data: {json.dumps({'type': 'error', 'content': 'Internal server error'})}\n\n"
    finally:
        await graph_stream.aclose()
    yield "data: [DONE]\n\n"


def _create_ai_message(parts: dict) -> AIMessage:
    sig = inspect.signature(AIMessage)
    valid_keys = set(sig.parameters)
    filtered = {k: v for k, v in parts.items() if k in valid_keys}
    return AIMessage(**filtered)


def _sse_response_example() -> dict[int | str, Any]:
    return {
        status.HTTP_200_OK: {
            "description": "Server Sent Event Response",
            "content": {
                "text/event-stream": {
                    "example": "data: {'type': 'token', 'content': 'Hello'}\n\ndata: {'type': 'token', 'content': ' World'}\n\ndata: [DONE]\n\n",
                    "schema": {"type": "string"},
                }
            },
        }
    }


@router.post(
    "/{agent_id}/stream",
    response_class=StreamingResponse,
    responses=_sse_response_example(),
    operation_id="stream_with_agent_id",
)
@router.post("/stream", response_class=StreamingResponse, responses=_sse_response_example())
async def stream(user_input: StreamInput, agent_id: str = DEFAULT_AGENT) -> StreamingResponse:
    """
    Stream an agent's response to a user input, including intermediate messages and tokens.

    If agent_id is not provided, the default agent will be used.
    Use thread_id to persist and continue a multi-turn conversation. run_id kwarg
    is also attached to all messages for recording feedback.
    Use user_id to persist and continue a conversation across multiple threads.

    Set `stream_tokens=false` to return intermediate messages but not token-by-token.
    """
    response_class = SupportStreamingResponse if agent_id == "support-agent" else StreamingResponse
    return response_class(
        message_generator(user_input, agent_id),
        media_type="text/event-stream",
    )


@router.get("/support-agent/approval")
async def pending_approval(thread_id: str, user_id: str | None = None) -> dict:
    async with execution_lock("support-agent", thread_id):
        await guard_thread(thread_id, user_id, "support-agent")
        snapshot = await get_agent("support-agent").aget_state(
            RunnableConfig(configurable={"thread_id": thread_id})
        )
        return {"pending": pending_payload(snapshot)}


@router.post("/feedback")
async def feedback(feedback: Feedback) -> FeedbackResponse:
    """
    Record feedback for a run to LangSmith.

    This is a simple wrapper for the LangSmith create_feedback API, so the
    credentials can be stored and managed in the service rather than the client.
    See: https://api.smith.langchain.com/redoc#tag/feedback/operation/create_feedback_api_v1_feedback_post
    """
    client = LangsmithClient()
    kwargs = feedback.kwargs or {}
    client.create_feedback(
        run_id=feedback.run_id,
        key=feedback.key,
        score=feedback.score,
        **kwargs,
    )
    return FeedbackResponse()


@router.post("/{agent_id}/history", operation_id="history_with_agent_id")
@router.post("/history")
async def history(input: ChatHistoryInput, agent_id: str = DEFAULT_AGENT) -> ChatHistory:
    """
    Get chat history for a thread and agent.

    If agent_id is not provided, the default agent will be used.
    """
    agent: AgentGraph = get_agent(agent_id)
    await guard_thread(input.thread_id, input.user_id, agent_id)
    config = RunnableConfig(configurable={"thread_id": input.thread_id})
    try:
        messages: list[BaseMessage] = []
        # Functional-API agents keep the conversation in `__previous__`, which aget_state
        # doesn't return, so read the raw checkpoint first and only fall back for graphs.
        checkpointer = getattr(agent, "checkpointer", None)
        if checkpointer:
            tup = await checkpointer.aget_tuple(config)
            if tup and "__previous__" in (tup.checkpoint.get("channel_values") or {}):
                messages = messages_from_checkpoint(tup.checkpoint)
        if not messages:
            state_snapshot = await agent.aget_state(config=config)
            messages = state_snapshot.values.get("messages", [])
        chat_messages: list[ChatMessage] = [langchain_to_chat_message(m) for m in messages]
        return ChatHistory(messages=chat_messages)
    except Exception as e:
        logger.error("History read failed: %s", type(e).__name__)
        raise HTTPException(status_code=500, detail="Unexpected error")


@router.get("/{agent_id}/threads", operation_id="threads_with_agent_id")
@router.get("/threads")
async def threads(
    input: UserThreadsInput = Depends(), agent_id: str = DEFAULT_AGENT
) -> UserThreads:
    """
    List a user's conversation threads for an agent, most recently updated first.

    `user_id` is asserted by the caller and not checked against the credentials on the
    request, so any holder of the bearer token can list any user's threads - the same
    trust model as /history. Put your own authorization in front of this before end
    users can reach it.
    """
    agent: AgentGraph = get_agent(agent_id)
    if agent_id == "support-agent":
        user_id = identity(input.user_id)
        try:
            rows = await support_repository().sessions(user_id, input.limit)
            from schema import ThreadSummary

            return UserThreads(
                threads=[
                    ThreadSummary(
                        thread_id=r["thread_id"],
                        agent_id=r["agent_id"],
                        title=r["title"],
                        updated_at=r["updated_at"],
                    )
                    for r in rows
                ]
            )
        except Exception:
            raise HTTPException(503, detail="Support session list unavailable") from None
    checkpointer = getattr(agent, "checkpointer", None)
    if not checkpointer:
        return UserThreads(threads=[])

    try:
        summaries = await list_user_threads(checkpointer, input.user_id, agent_id, input.limit)
    except Exception as e:
        logger.error("Thread list read failed: %s", type(e).__name__)
        raise HTTPException(status_code=500, detail="Unexpected error")

    return UserThreads(threads=summaries)


@router.get("/support-agent/preferences")
async def preferences_get(user_id: str):
    return await read_preferences(
        getattr(get_agent("support-agent"), "store", None), identity(user_id)
    )


@router.put("/support-agent/preferences")
async def preferences_put(preferences: Preferences, user_id: str):
    user_id = identity(user_id)
    try:
        return await save_preferences(
            getattr(get_agent("support-agent"), "store", None), user_id, preferences
        )
    except Exception:
        raise HTTPException(503, detail="Preferences were not saved") from None


@router.delete("/support-agent/preferences")
async def preferences_delete(user_id: str):
    user_id = identity(user_id)
    try:
        return await delete_preferences(getattr(get_agent("support-agent"), "store", None), user_id)
    except Exception:
        raise HTTPException(503, detail="Preference deletion was not confirmed") from None


@router.get("/support-agent/tickets")
async def tickets_list(user_id: str, limit: int = 20):
    user_id = identity(user_id)
    if not 1 <= limit <= 100:
        raise HTTPException(422, detail="limit must be 1..100")
    try:
        return {
            "tickets": [r.model_dump() for r in await support_repository().tickets(user_id, limit)],
            "is_demo": True,
        }
    except Exception:
        raise HTTPException(503, detail="Ticket storage unavailable") from None


@app.get("/health")
async def health_check():
    """Health check endpoint."""

    health_status = {"status": "ok"}

    if settings.LANGFUSE_TRACING:
        try:
            langfuse = Langfuse()
            health_status["langfuse"] = "connected" if langfuse.auth_check() else "disconnected"
        except Exception as e:
            logger.error(f"Langfuse connection error: {e}")
            health_status["langfuse"] = "disconnected"

    return health_status


app.include_router(router)
