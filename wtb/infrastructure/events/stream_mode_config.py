"""
LangGraph Stream Mode Configuration.

Provides environment-specific stream mode configurations for LangGraph event streaming.
Follows SOLID principles with clear separation of concerns.

Stream Modes (from LangGraph):
- "values": Full state after each node
- "updates": Delta changes per node  
- "messages": LLM tokens as they stream
- "debug": Detailed internal events
- "events": Lifecycle events (v2 API)
- "custom": User-defined events via get_stream_writer()

Usage:
    from wtb.infrastructure.events import StreamModeConfig
    
    # Get modes for environment
    modes = StreamModeConfig.for_production()
    
    # Use in graph execution
    async for event in graph.astream_events(state, config, stream_mode=modes):
        ...
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StreamMode(Enum):
    """
    LangGraph stream modes.
    
    Each mode provides different event granularity:
    - VALUES: Full state snapshot after each node
    - UPDATES: Only changed values per node
    - MESSAGES: LLM token-by-token streaming
    - DEBUG: Internal graph execution details
    - EVENTS: Lifecycle events (recommended for audit)
    - CUSTOM: Application-specific events
    """
    VALUES = "values"
    UPDATES = "updates"
    MESSAGES = "messages"
    DEBUG = "debug"
    EVENTS = "events"
    CUSTOM = "custom"


@dataclass
class StreamModeConfig:
    """
    Configuration for LangGraph stream modes.
    
    Supports environment-specific presets and custom configurations.
    
    SOLID Compliance:
    - SRP: Single responsibility for stream mode configuration
    - OCP: Extensible via custom modes without modifying existing presets
    
    Attributes:
        modes: List of enabled stream modes
        include_inputs: Include input state in events (may contain PII)
        include_outputs: Include output state in events
        filter_internal: Filter internal LangGraph nodes (prefixed with __)
        max_token_count: Max LLM tokens to capture per event (for messages mode)
        custom_event_types: List of custom event types to capture
    """
    modes: list[str] = field(default_factory=lambda: ["updates"])
    include_inputs: bool = False
    include_outputs: bool = True
    filter_internal: bool = True
    max_token_count: int | None = None
    custom_event_types: list[str] = field(default_factory=list)
    
    # Event filtering
    node_name_filter: list[str] | None = None  # Only capture these nodes
    exclude_node_names: list[str] | None = None  # Exclude these nodes
    
    # Timing
    emit_timing_events: bool = True
    
    @classmethod
    def for_testing(cls) -> "StreamModeConfig":
        """
        Minimal streaming for unit tests.
        
        Fast execution with only essential updates.
        
        Returns:
            StreamModeConfig with minimal modes
        """
        return cls(
            modes=["updates"],
            include_inputs=False,
            include_outputs=True,
            filter_internal=True,
            emit_timing_events=False,
        )
    
    @classmethod
    def for_development(cls) -> "StreamModeConfig":
        """
        Detailed streaming for debugging.
        
        Captures full state and debug events for development.
        
        Returns:
            StreamModeConfig with detailed modes
        """
        return cls(
            modes=["values", "updates", "debug", "custom"],
            include_inputs=True,
            include_outputs=True,
            filter_internal=False,
            emit_timing_events=True,
        )
    
    @classmethod
    def for_production(cls) -> "StreamModeConfig":
        """
        Balanced streaming for production audit.
        
        Captures updates and custom events without overhead.
        
        Returns:
            StreamModeConfig for production
        """
        return cls(
            modes=["updates", "custom"],
            include_inputs=False,  # PII protection
            include_outputs=True,
            filter_internal=True,
            emit_timing_events=True,
        )
    
    @classmethod
    def for_full_audit(cls) -> "StreamModeConfig":
        """
        Complete audit trail with all events.
        
        Maximum detail for compliance and debugging.
        WARNING: May impact performance and storage.
        
        Returns:
            StreamModeConfig with all modes
        """
        return cls(
            modes=["values", "updates", "messages", "debug", "events", "custom"],
            include_inputs=True,
            include_outputs=True,
            filter_internal=False,
            emit_timing_events=True,
            max_token_count=1000,  # Limit token capture for storage
        )
    
    @classmethod
    def for_llm_streaming(cls) -> "StreamModeConfig":
        """
        Configuration for LLM token streaming.
        
        Ideal for UI streaming responses.
        
        Returns:
            StreamModeConfig with message streaming
        """
        return cls(
            modes=["messages", "updates"],
            include_inputs=False,
            include_outputs=True,
            filter_internal=True,
            emit_timing_events=False,
        )
    
    @classmethod
    def custom(
        cls,
        modes: list[str],
        include_inputs: bool = False,
        include_outputs: bool = True,
        filter_internal: bool = True,
    ) -> "StreamModeConfig":
        """
        Create custom configuration.
        
        Args:
            modes: List of stream modes to enable
            include_inputs: Include input state
            include_outputs: Include output state
            filter_internal: Filter internal nodes
            
        Returns:
            Custom StreamModeConfig
        """
        return cls(
            modes=modes,
            include_inputs=include_inputs,
            include_outputs=include_outputs,
            filter_internal=filter_internal,
        )
    
    def get_modes(self) -> list[str]:
        """
        Get list of enabled stream modes.
        
        Returns:
            List of mode strings for LangGraph
        """
        return self.modes.copy()
    
    def should_include_event(self, event: dict[str, Any]) -> bool:
        """
        Check if event should be included based on filters.
        
        Args:
            event: LangGraph streaming event
            
        Returns:
            True if event should be captured
        """
        name = event.get("name", "")
        
        # Filter internal nodes
        if self.filter_internal and name.startswith("__"):
            return False
        
        # Filter by node name
        if self.node_name_filter and name not in self.node_name_filter:
            return False
        
        # Exclude specific nodes
        if self.exclude_node_names and name in self.exclude_node_names:
            return False
        
        return True
    
    def to_dict(self) -> dict[str, Any]:
        """
        Serialize to dictionary.
        
        Returns:
            Dictionary representation
        """
        return {
            "modes": self.modes,
            "include_inputs": self.include_inputs,
            "include_outputs": self.include_outputs,
            "filter_internal": self.filter_internal,
            "max_token_count": self.max_token_count,
            "custom_event_types": self.custom_event_types,
            "node_name_filter": self.node_name_filter,
            "exclude_node_names": self.exclude_node_names,
            "emit_timing_events": self.emit_timing_events,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StreamModeConfig":
        """
        Deserialize from dictionary.
        
        Args:
            data: Dictionary with config values
            
        Returns:
            StreamModeConfig instance
        """
        return cls(
            modes=data.get("modes", ["updates"]),
            include_inputs=data.get("include_inputs", False),
            include_outputs=data.get("include_outputs", True),
            filter_internal=data.get("filter_internal", True),
            max_token_count=data.get("max_token_count"),
            custom_event_types=data.get("custom_event_types", []),
            node_name_filter=data.get("node_name_filter"),
            exclude_node_names=data.get("exclude_node_names"),
            emit_timing_events=data.get("emit_timing_events", True),
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Environment Detection
# ═══════════════════════════════════════════════════════════════════════════════

def get_stream_mode_for_environment(env: str = "production") -> StreamModeConfig:
    """
    Get appropriate stream mode configuration for environment.
    
    Args:
        env: Environment name ("testing", "development", "production", "audit")
        
    Returns:
        StreamModeConfig for the environment
    """
    configs = {
        "testing": StreamModeConfig.for_testing,
        "development": StreamModeConfig.for_development,
        "production": StreamModeConfig.for_production,
        "audit": StreamModeConfig.for_full_audit,
        "streaming": StreamModeConfig.for_llm_streaming,
    }
    
    factory = configs.get(env.lower(), StreamModeConfig.for_production)
    return factory()
