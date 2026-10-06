from dataclasses import dataclass
from importlib import import_module

from langgraph.graph.state import CompiledStateGraph
from langgraph.pregel import Pregel

from agents.lazy_agent import LazyLoadingAgent
from agents.support_agent import support_agent
from core import settings
from schema import AgentInfo

DEFAULT_AGENT = "support-agent"

# Type alias to handle LangGraph's different agent patterns
# - @entrypoint functions return Pregel
# - StateGraph().compile() returns CompiledStateGraph
AgentGraph = CompiledStateGraph | Pregel  # What get_agent() returns (always loaded)
AgentGraphLike = CompiledStateGraph | Pregel | LazyLoadingAgent  # What can be stored in registry


class ModuleAgent(LazyLoadingAgent):
    def __init__(self, module, attribute):
        super().__init__()
        self.module, self.attribute = module, attribute

    async def load(self):
        if self._loaded:
            return
        graph = getattr(import_module(self.module), self.attribute)
        if isinstance(graph, LazyLoadingAgent):
            await graph.load()
            graph = graph.get_graph()
        self._graph, self._loaded = graph, True


def enabled(agent_id):
    return agent_id in settings.ENABLED_AGENTS


@dataclass
class Agent:
    description: str
    graph_like: AgentGraphLike


agents: dict[str, Agent] = {
    "support-agent": Agent(
        description="Enterprise IT support with knowledge retrieval, explicit preferences and approval-gated demonstration tickets.",
        graph_like=support_agent,
    ),
    "chatbot": Agent(
        description="A simple chatbot.", graph_like=ModuleAgent("agents.chatbot", "chatbot")
    ),
    "research-assistant": Agent(
        description="A research assistant with web search and calculator.",
        graph_like=ModuleAgent("agents.research_assistant", "research_assistant"),
    ),
    "rag-assistant": Agent(
        description="A RAG assistant with access to information in a database.",
        graph_like=ModuleAgent("agents.rag_assistant", "rag_assistant"),
    ),
    "command-agent": Agent(
        description="A command agent.",
        graph_like=ModuleAgent("agents.command_agent", "command_agent"),
    ),
    "bg-task-agent": Agent(
        description="A background task agent.",
        graph_like=ModuleAgent("agents.bg_task_agent.bg_task_agent", "bg_task_agent"),
    ),
    "langgraph-supervisor-agent": Agent(
        description="A langgraph supervisor agent",
        graph_like=ModuleAgent("agents.langgraph_supervisor_agent", "langgraph_supervisor_agent"),
    ),
    "langgraph-supervisor-hierarchy-agent": Agent(
        description="A langgraph supervisor agent with a nested hierarchy of agents",
        graph_like=ModuleAgent(
            "agents.langgraph_supervisor_hierarchy_agent", "langgraph_supervisor_hierarchy_agent"
        ),
    ),
    "interrupt-agent": Agent(
        description="An agent the uses interrupts.",
        graph_like=ModuleAgent("agents.interrupt_agent", "interrupt_agent"),
    ),
    "knowledge-base-agent": Agent(
        description="A retrieval-augmented generation agent using Amazon Bedrock Knowledge Base",
        graph_like=ModuleAgent("agents.knowledge_base_agent", "kb_agent"),
    ),
    "github-mcp-agent": Agent(
        description="A GitHub agent with MCP tools for repository management and development workflows.",
        graph_like=ModuleAgent("agents.github_mcp_agent.github_mcp_agent", "github_mcp_agent"),
    ),
}


async def load_agent(agent_id: str) -> None:
    """Load lazy agents if needed."""
    if not enabled(agent_id):
        raise KeyError(agent_id)
    graph_like = agents[agent_id].graph_like
    if isinstance(graph_like, LazyLoadingAgent):
        await graph_like.load()


def get_agent(agent_id: str) -> AgentGraph:
    """Get an agent graph, loading lazy agents if needed."""
    if not enabled(agent_id):
        raise KeyError(agent_id)
    agent_graph = agents[agent_id].graph_like

    # If it's a lazy loading agent, ensure it's loaded and return its graph
    if isinstance(agent_graph, LazyLoadingAgent):
        if not agent_graph._loaded:
            raise RuntimeError(f"Agent {agent_id} not loaded. Call load() first.")
        return agent_graph.get_graph()

    # Otherwise return the graph directly
    return agent_graph


def get_all_agent_info() -> list[AgentInfo]:
    if set(settings.ENABLED_AGENTS) - set(agents):
        raise ValueError("Unknown enabled agent")
    return [
        AgentInfo(key=agent_id, description=agent.description)
        for agent_id, agent in agents.items()
        if enabled(agent_id)
    ]
