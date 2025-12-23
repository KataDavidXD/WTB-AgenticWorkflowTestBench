"""
SQLAlchemy ORM Models for WTB.

These models define the WTB-owned tables in wtb.db.
AgentGit tables remain unchanged in agentgit.db.
"""

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Float,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    CheckConstraint,
)
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime
import json

Base = declarative_base()


# ═══════════════════════════════════════════════════════════════════════════════
# Helper for JSON serialization
# ═══════════════════════════════════════════════════════════════════════════════

class JSONEncodedDict(Text):
    """Custom type for storing JSON as TEXT."""
    pass


def json_serializer(obj):
    """Serialize object to JSON string."""
    if obj is None:
        return None
    return json.dumps(obj)


def json_deserializer(s):
    """Deserialize JSON string to object."""
    if s is None:
        return None
    return json.loads(s)


# ═══════════════════════════════════════════════════════════════════════════════
# WTB Core Models
# ═══════════════════════════════════════════════════════════════════════════════

class WorkflowORM(Base):
    """ORM model for wtb_workflows table."""
    
    __tablename__ = 'wtb_workflows'
    
    id = Column(String(64), primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    definition = Column(Text, nullable=False)  # JSON: workflow graph
    version = Column(String(50), default='1.0.0')
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, onupdate=datetime.utcnow)
    metadata_ = Column('metadata', Text, nullable=True)  # JSON
    
    # Relationships
    executions = relationship('ExecutionORM', back_populates='workflow')
    node_variants = relationship('NodeVariantORM', back_populates='workflow')
    batch_tests = relationship('BatchTestORM', back_populates='workflow')
    
    __table_args__ = (
        Index('idx_wtb_workflows_name', 'name'),
    )


class ExecutionORM(Base):
    """ORM model for wtb_executions table."""
    
    __tablename__ = 'wtb_executions'
    
    id = Column(String(64), primary_key=True)
    workflow_id = Column(String(64), ForeignKey('wtb_workflows.id'), nullable=False)
    
    # Status
    status = Column(String(20), nullable=False, default='pending')
    current_node_id = Column(String(255), nullable=True)
    
    # State (JSON)
    initial_state = Column(Text, nullable=True)
    current_state = Column(Text, nullable=True)
    execution_path = Column(Text, nullable=True)  # JSON: list of node IDs
    
    # AgentGit integration (cross-database references)
    agentgit_session_id = Column(Integer, nullable=True)
    agentgit_checkpoint_id = Column(Integer, nullable=True)
    
    # Timing
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Error handling
    error_message = Column(Text, nullable=True)
    error_node_id = Column(String(255), nullable=True)
    
    # Breakpoints (JSON list)
    breakpoints = Column(Text, nullable=True)
    
    # Metadata (JSON)
    metadata_ = Column('metadata', Text, nullable=True)
    
    # Relationships
    workflow = relationship('WorkflowORM', back_populates='executions')
    evaluation_results = relationship('EvaluationResultORM', back_populates='execution')
    
    __table_args__ = (
        Index('idx_wtb_executions_workflow', 'workflow_id'),
        Index('idx_wtb_executions_status', 'status'),
        CheckConstraint(
            "status IN ('pending', 'running', 'paused', 'completed', 'failed', 'cancelled')",
            name='ck_execution_status'
        ),
    )


class NodeVariantORM(Base):
    """ORM model for wtb_node_variants table."""
    
    __tablename__ = 'wtb_node_variants'
    
    id = Column(String(64), primary_key=True)
    workflow_id = Column(String(64), ForeignKey('wtb_workflows.id'), nullable=False)
    original_node_id = Column(String(255), nullable=False)
    variant_name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    
    # Variant definition (JSON)
    variant_definition = Column(Text, nullable=False)
    
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    metadata_ = Column('metadata', Text, nullable=True)
    
    # Relationships
    workflow = relationship('WorkflowORM', back_populates='node_variants')
    
    __table_args__ = (
        UniqueConstraint('workflow_id', 'original_node_id', 'variant_name', 
                        name='uq_workflow_node_variant'),
        Index('idx_wtb_node_variants_workflow', 'workflow_id'),
    )


class BatchTestORM(Base):
    """ORM model for wtb_batch_tests table."""
    
    __tablename__ = 'wtb_batch_tests'
    
    id = Column(String(64), primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    workflow_id = Column(String(64), ForeignKey('wtb_workflows.id'), nullable=False)
    
    # Configuration (JSON)
    variant_combinations = Column(Text, nullable=False)
    initial_state = Column(Text, nullable=True)
    parallel_count = Column(Integer, default=1)
    
    # Status
    status = Column(String(20), nullable=False, default='pending')
    
    # Timing
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    
    # Results (JSON)
    execution_ids = Column(Text, nullable=True)
    results = Column(Text, nullable=True)
    comparison_matrix = Column(Text, nullable=True)
    best_combination_name = Column(String(255), nullable=True)
    
    # Metadata (JSON)
    metadata_ = Column('metadata', Text, nullable=True)
    
    # Relationships
    workflow = relationship('WorkflowORM', back_populates='batch_tests')
    
    __table_args__ = (
        Index('idx_wtb_batch_tests_workflow', 'workflow_id'),
        Index('idx_wtb_batch_tests_status', 'status'),
    )


class EvaluationResultORM(Base):
    """ORM model for wtb_evaluation_results table."""
    
    __tablename__ = 'wtb_evaluation_results'
    
    id = Column(String(64), primary_key=True)
    execution_id = Column(String(64), ForeignKey('wtb_executions.id'), nullable=False)
    batch_test_id = Column(String(64), nullable=True)
    
    # Evaluator info
    evaluator_name = Column(String(255), nullable=False)
    evaluator_version = Column(String(50), default='1.0.0')
    
    # Results
    overall_score = Column(Float, default=0.0)
    passed = Column(Boolean, default=True)
    metrics = Column(Text, nullable=True)  # JSON
    details = Column(Text, nullable=True)
    raw_output = Column(Text, nullable=True)  # JSON
    
    # Timing
    evaluated_at = Column(DateTime, default=datetime.utcnow)
    evaluation_duration_ms = Column(Integer, default=0)
    
    # Metadata (JSON)
    metadata_ = Column('metadata', Text, nullable=True)
    
    # Relationships
    execution = relationship('ExecutionORM', back_populates='evaluation_results')
    
    __table_args__ = (
        Index('idx_wtb_evaluation_results_execution', 'execution_id'),
        Index('idx_wtb_evaluation_results_evaluator', 'evaluator_name'),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# WTB Anti-Corruption Layer Models (Bridge to AgentGit/FileTracker)
# ═══════════════════════════════════════════════════════════════════════════════

class NodeBoundaryORM(Base):
    """ORM model for wtb_node_boundaries table."""
    
    __tablename__ = 'wtb_node_boundaries'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # WTB Execution Context
    execution_id = Column(String(64), nullable=False)
    
    # AgentGit Reference (cross-database, logical FK)
    internal_session_id = Column(Integer, nullable=False)
    
    # Node Identification
    node_id = Column(String(255), nullable=False)
    
    # Checkpoint Pointers (cross-database to agentgit.checkpoints.id)
    entry_checkpoint_id = Column(Integer, nullable=True)
    exit_checkpoint_id = Column(Integer, nullable=True)
    
    # Node Execution Status
    node_status = Column(String(20), nullable=False, default='started')
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    
    # Metrics
    tool_count = Column(Integer, default=0)
    checkpoint_count = Column(Integer, default=0)
    duration_ms = Column(Integer, nullable=True)
    
    # Error Info
    error_message = Column(Text, nullable=True)
    error_details = Column(Text, nullable=True)  # JSON
    
    __table_args__ = (
        UniqueConstraint('internal_session_id', 'node_id', name='uq_session_node'),
        Index('idx_wtb_node_boundaries_session', 'internal_session_id'),
        Index('idx_wtb_node_boundaries_execution', 'execution_id'),
        Index('idx_wtb_node_boundaries_status', 'node_status'),
        CheckConstraint(
            "node_status IN ('started', 'completed', 'failed', 'skipped')",
            name='ck_node_status'
        ),
    )


class CheckpointFileORM(Base):
    """ORM model for wtb_checkpoint_files table."""
    
    __tablename__ = 'wtb_checkpoint_files'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # AgentGit Reference (cross-database, logical FK)
    checkpoint_id = Column(Integer, nullable=False, unique=True)
    
    # FileTracker Reference (cross-database, logical FK)
    file_commit_id = Column(String(64), nullable=False)
    
    # Summary (denormalized)
    file_count = Column(Integer, default=0)
    total_size_bytes = Column(Integer, default=0)
    
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    
    __table_args__ = (
        Index('idx_wtb_checkpoint_files_checkpoint', 'checkpoint_id'),
        Index('idx_wtb_checkpoint_files_commit', 'file_commit_id'),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Outbox Pattern Model
# ═══════════════════════════════════════════════════════════════════════════════

class OutboxEventORM(Base):
    """
    ORM model for wtb_outbox table.
    
    Implements the Outbox Pattern for cross-database transaction consistency.
    Events are written atomically with business data in the same transaction,
    then processed asynchronously by OutboxProcessor.
    """
    
    __tablename__ = 'wtb_outbox'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # Event identification
    event_id = Column(String(64), nullable=False, unique=True)  # UUID for idempotency
    event_type = Column(String(50), nullable=False)
    aggregate_type = Column(String(50), nullable=False)
    aggregate_id = Column(String(64), nullable=False)
    
    # Event payload (JSON)
    payload = Column(Text, nullable=False)
    
    # Processing status
    status = Column(String(20), nullable=False, default='pending')
    retry_count = Column(Integer, nullable=False, default=0)
    max_retries = Column(Integer, nullable=False, default=5)
    
    # Timestamps
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)
    
    # Error tracking
    last_error = Column(Text, nullable=True)
    
    __table_args__ = (
        Index('idx_wtb_outbox_status_created', 'status', 'created_at'),
        Index('idx_wtb_outbox_event_id', 'event_id'),
        Index('idx_wtb_outbox_aggregate', 'aggregate_type', 'aggregate_id'),
        CheckConstraint(
            "status IN ('pending', 'processing', 'processed', 'failed')",
            name='ck_outbox_status'
        ),
    )