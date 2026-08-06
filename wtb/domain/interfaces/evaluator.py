"""
Evaluator Interfaces.

Defines contracts for evaluating workflow execution results.
Supports extensible evaluation strategies via plugin pattern.
"""

import builtins
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvaluationMetric:
    """Single evaluation metric."""
    name: str
    value: float
    unit: str | None = None
    description: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluationScore:
    """Complete evaluation result from an evaluator."""
    evaluator_name: str
    overall_score: float  # 0.0 to 1.0 normalized
    metrics: list[EvaluationMetric] = field(default_factory=list)
    passed: bool = True
    details: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def get_metric(self, name: str) -> EvaluationMetric | None:
        """Get a specific metric by name."""
        for metric in self.metrics:
            if metric.name == name:
                return metric
        return None


class IEvaluator(ABC):
    """
    Interface for evaluating workflow execution.
    
    Implementations:
    - AccuracyEvaluator: Measures output correctness
    - LatencyEvaluator: Measures execution time
    - CostEvaluator: Measures resource consumption
    - CustomEvaluator: User-defined evaluation logic
    """
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Evaluator name for identification."""
        pass
    
    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description."""
        pass
    
    @abstractmethod
    def evaluate(
        self,
        execution_result: dict[str, Any],
        expected_result: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None
    ) -> EvaluationScore:
        """
        Evaluate an execution result.
        
        Args:
            execution_result: The actual output from execution
            expected_result: Optional expected/ground truth output
            context: Optional additional context (workflow config, etc.)
            
        Returns:
            EvaluationScore with metrics and overall score
        """
        pass
    
    @abstractmethod
    def validate_config(self, config: dict[str, Any]) -> list[str]:
        """
        Validate evaluator configuration.
        
        Args:
            config: Configuration dictionary
            
        Returns:
            List of validation error messages (empty if valid)
        """
        pass


class IEvaluatorRegistry(ABC):
    """
    Registry for evaluators.
    
    Manages available evaluators and provides composite evaluation.
    """
    
    @abstractmethod
    def register(self, evaluator: IEvaluator):
        """
        Register an evaluator.
        
        Args:
            evaluator: Evaluator instance
            
        Raises:
            ValueError: If evaluator with same name already exists
        """
        pass
    
    @abstractmethod
    def unregister(self, name: str) -> bool:
        """
        Unregister an evaluator by name.
        
        Args:
            name: Evaluator name
            
        Returns:
            True if removed, False if not found
        """
        pass
    
    @abstractmethod
    def get(self, name: str) -> IEvaluator | None:
        """
        Get evaluator by name.
        
        Args:
            name: Evaluator name
            
        Returns:
            IEvaluator if found, None otherwise
        """
        pass
    
    @abstractmethod
    def list(self) -> list[str]:
        """
        List all registered evaluator names.
        
        Returns:
            List of evaluator names
        """
        pass
    
    @abstractmethod
    def evaluate_all(
        self,
        execution_result: dict[str, Any],
        expected_result: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        evaluator_names: builtins.list[str] | None = None
    ) -> builtins.list[EvaluationScore]:
        """
        Run all (or specified) evaluators.
        
        Args:
            execution_result: The actual output from execution
            expected_result: Optional expected/ground truth output
            context: Optional additional context
            evaluator_names: Optional list of evaluator names to run
                           (if None, runs all)
            
        Returns:
            List of EvaluationScore from each evaluator
        """
        pass


class IEvaluationEngine(ABC):
    """
    High-level evaluation engine.
    
    Orchestrates evaluation of executions and batch tests.
    """
    
    @abstractmethod
    def evaluate_execution(
        self,
        execution_id: str,
        evaluator_names: list[str] | None = None
    ) -> list[EvaluationScore]:
        """
        Evaluate a single execution.
        
        Args:
            execution_id: ID of the execution to evaluate
            evaluator_names: Optional list of evaluators to use
            
        Returns:
            List of evaluation scores
            
        Raises:
            ValueError: If execution not found
        """
        pass
    
    @abstractmethod
    def evaluate_batch(
        self,
        batch_test_id: str,
        evaluator_names: list[str] | None = None
    ) -> dict[str, list[EvaluationScore]]:
        """
        Evaluate all executions in a batch test.
        
        Args:
            batch_test_id: ID of the batch test
            evaluator_names: Optional list of evaluators to use
            
        Returns:
            Dict mapping execution_id to list of scores
            
        Raises:
            ValueError: If batch test not found
        """
        pass
    
    @abstractmethod
    def compare_executions(
        self,
        execution_ids: list[str],
        evaluator_names: list[str] | None = None
    ) -> dict[str, Any]:
        """
        Compare multiple executions.
        
        Args:
            execution_ids: List of execution IDs to compare
            evaluator_names: Optional list of evaluators to use
            
        Returns:
            Comparison matrix with scores and rankings
        """
        pass
    
    @abstractmethod
    def get_aggregate_score(
        self,
        scores: list[EvaluationScore],
        weights: dict[str, float] | None = None
    ) -> float:
        """
        Calculate aggregate score from multiple evaluator scores.
        
        Args:
            scores: List of evaluation scores
            weights: Optional weights for each evaluator (by name)
            
        Returns:
            Weighted aggregate score (0.0 to 1.0)
        """
        pass

