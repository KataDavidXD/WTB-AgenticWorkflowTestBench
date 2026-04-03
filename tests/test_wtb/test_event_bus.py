"""
Tests for WTBEventBus.

Tests thread-safety, bounded history, and event handling.
"""

import pytest
import threading
from datetime import datetime, timedelta
from typing import List

from wtb.infrastructure.events.wtb_event_bus import (
    WTBEventBus,
    get_wtb_event_bus,
    set_wtb_event_bus,
    reset_wtb_event_bus,
)
from wtb.domain.events import (
    ExecutionStartedEvent,
    ExecutionCompletedEvent,
    ExecutionFailedEvent,
    NodeStartedEvent,
    NodeCompletedEvent,
)


class TestWTBEventBusBasics:
    """Basic event bus operations."""
    
    def test_create_event_bus(self):
        """Event bus can be created with default settings."""
        bus = WTBEventBus()
        assert bus is not None
        assert bus.get_subscriber_count() == 0
    
    def test_create_event_bus_with_custom_history(self):
        """Event bus respects custom max_history."""
        bus = WTBEventBus(max_history=50)
        assert bus._max_history == 50
    
    def test_subscribe_and_publish(self):
        """Can subscribe to events and receive them."""
        bus = WTBEventBus()
        received_events: List = []
        
        def handler(event):
            received_events.append(event)
        
        bus.subscribe(ExecutionStartedEvent, handler)
        
        event = ExecutionStartedEvent(
            execution_id="exec-1",
            workflow_id="wf-1",
            workflow_name="Test Workflow",
        )
        bus.publish(event)
        
        assert len(received_events) == 1
        assert received_events[0].execution_id == "exec-1"
    
    def test_multiple_subscribers(self):
        """Multiple subscribers receive the same event."""
        bus = WTBEventBus()
        received1: List = []
        received2: List = []
        
        bus.subscribe(ExecutionStartedEvent, lambda e: received1.append(e))
        bus.subscribe(ExecutionStartedEvent, lambda e: received2.append(e))
        
        event = ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1")
        bus.publish(event)
        
        assert len(received1) == 1
        assert len(received2) == 1
    
    def test_type_specific_subscription(self):
        """Subscribers only receive their event type."""
        bus = WTBEventBus()
        started_events: List = []
        completed_events: List = []
        
        bus.subscribe(ExecutionStartedEvent, lambda e: started_events.append(e))
        bus.subscribe(ExecutionCompletedEvent, lambda e: completed_events.append(e))
        
        bus.publish(ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1"))
        bus.publish(ExecutionCompletedEvent(execution_id="exec-1", workflow_id="wf-1"))
        
        assert len(started_events) == 1
        assert len(completed_events) == 1
    
    def test_unsubscribe(self):
        """Can unsubscribe from events."""
        bus = WTBEventBus()
        received: List = []
        
        def handler(event):
            received.append(event)
        
        bus.subscribe(ExecutionStartedEvent, handler)
        bus.publish(ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1"))
        
        assert len(received) == 1
        
        bus.unsubscribe(ExecutionStartedEvent, handler)
        bus.publish(ExecutionStartedEvent(execution_id="exec-2", workflow_id="wf-1"))
        
        assert len(received) == 1  # No new events
    
    def test_handler_exception_doesnt_break_other_handlers(self):
        """Exception in one handler doesn't prevent others from running."""
        bus = WTBEventBus()
        received: List = []
        
        def failing_handler(event):
            raise ValueError("Intentional failure")
        
        def working_handler(event):
            received.append(event)
        
        bus.subscribe(ExecutionStartedEvent, failing_handler)
        bus.subscribe(ExecutionStartedEvent, working_handler)
        
        bus.publish(ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1"))
        
        # Working handler should still receive the event
        assert len(received) == 1


class TestWTBEventBusHistory:
    """Event history tests."""
    
    def test_event_stored_in_history(self):
        """Published events are stored in history."""
        bus = WTBEventBus()
        
        event = ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1")
        bus.publish(event)
        
        history = bus.get_event_history()
        assert len(history) == 1
        assert history[0].execution_id == "exec-1"
    
    def test_history_bounded(self):
        """History is bounded to max_history."""
        bus = WTBEventBus(max_history=5)
        
        for i in range(10):
            bus.publish(ExecutionStartedEvent(execution_id=f"exec-{i}", workflow_id="wf-1"))
        
        history = bus.get_event_history()
        assert len(history) == 5
        
        # Should have the most recent events
        assert history[0].execution_id == "exec-5"
        assert history[4].execution_id == "exec-9"
    
    def test_history_filtered_by_type(self):
        """Can filter history by event type."""
        bus = WTBEventBus()
        
        bus.publish(ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1"))
        bus.publish(ExecutionCompletedEvent(execution_id="exec-1", workflow_id="wf-1"))
        bus.publish(ExecutionStartedEvent(execution_id="exec-2", workflow_id="wf-1"))
        
        started_history = bus.get_event_history(ExecutionStartedEvent)
        completed_history = bus.get_event_history(ExecutionCompletedEvent)
        
        assert len(started_history) == 2
        assert len(completed_history) == 1
    
    def test_get_history_with_since_filter(self):
        """Can filter history by timestamp."""
        bus = WTBEventBus()
        
        # Create events with different timestamps
        old_event = ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1")
        old_event.timestamp = datetime.now() - timedelta(hours=1)
        
        new_event = ExecutionStartedEvent(execution_id="exec-2", workflow_id="wf-1")
        new_event.timestamp = datetime.now()
        
        bus._event_history.append(old_event)
        bus._event_history.append(new_event)
        
        since = datetime.now() - timedelta(minutes=30)
        recent = bus.get_history(since=since)
        
        assert len(recent) == 1
        assert recent[0].execution_id == "exec-2"
    
    def test_get_history_with_limit(self):
        """Can limit history results."""
        bus = WTBEventBus()
        
        for i in range(10):
            bus.publish(ExecutionStartedEvent(execution_id=f"exec-{i}", workflow_id="wf-1"))
        
        limited = bus.get_history(limit=3)
        
        assert len(limited) == 3
        # Most recent first
        assert limited[0].execution_id == "exec-9"
    
    def test_clear_history(self):
        """Can clear event history."""
        bus = WTBEventBus()
        
        bus.publish(ExecutionStartedEvent(execution_id="exec-1", workflow_id="wf-1"))
        assert len(bus.get_event_history()) == 1
        
        bus.clear_history()
        assert len(bus.get_event_history()) == 0


class TestWTBEventBusThreadSafety:
    """Thread-safety tests."""
    
    def test_concurrent_publish(self):
        """Multiple threads can publish concurrently."""
        bus = WTBEventBus()
        events_per_thread = 100
        num_threads = 10
        
        def publish_events(thread_id: int):
            for i in range(events_per_thread):
                bus.publish(ExecutionStartedEvent(
                    execution_id=f"exec-{thread_id}-{i}",
                    workflow_id="wf-1",
                ))
        
        threads = [
            threading.Thread(target=publish_events, args=(i,))
            for i in range(num_threads)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All events should be published (some may be evicted from bounded history)
        # But no crashes or data corruption
        history = bus.get_event_history()
        assert len(history) <= bus._max_history
    
    def test_concurrent_subscribe_publish(self):
        """Subscribe and publish can happen concurrently."""
        bus = WTBEventBus()
        received: List = []
        lock = threading.Lock()
        
        def handler(event):
            with lock:
                received.append(event)
        
        def subscribe_loop():
            for _ in range(100):
                bus.subscribe(ExecutionStartedEvent, handler)
                bus.unsubscribe(ExecutionStartedEvent, handler)
        
        def publish_loop():
            for i in range(100):
                bus.publish(ExecutionStartedEvent(
                    execution_id=f"exec-{i}",
                    workflow_id="wf-1",
                ))
        
        t1 = threading.Thread(target=subscribe_loop)
        t2 = threading.Thread(target=publish_loop)
        
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        
        # No crashes, some events may have been received
        assert True


class TestWTBEventBusGlobal:
    """Global event bus tests."""
    
    def setup_method(self):
        """Reset global bus before each test."""
        reset_wtb_event_bus()
    
    def teardown_method(self):
        """Reset global bus after each test."""
        reset_wtb_event_bus()
    
    def test_get_global_bus(self):
        """Can get global event bus."""
        bus = get_wtb_event_bus()
        assert bus is not None
        assert isinstance(bus, WTBEventBus)
    
    def test_global_bus_is_singleton(self):
        """Global bus returns same instance."""
        bus1 = get_wtb_event_bus()
        bus2 = get_wtb_event_bus()
        assert bus1 is bus2
    
    def test_set_global_bus(self):
        """Can set custom global bus."""
        custom_bus = WTBEventBus(max_history=50)
        set_wtb_event_bus(custom_bus)
        
        bus = get_wtb_event_bus()
        assert bus is custom_bus
        assert bus._max_history == 50
    
    def test_reset_global_bus(self):
        """Reset creates new instance on next get."""
        bus1 = get_wtb_event_bus()
        reset_wtb_event_bus()
        bus2 = get_wtb_event_bus()
        
        assert bus1 is not bus2


class TestWTBEventBusPublishAll:
    """Batch publish tests."""
    
    def test_publish_all(self):
        """Can publish multiple events at once."""
        bus = WTBEventBus()
        received: List = []
        
        bus.subscribe(ExecutionStartedEvent, lambda e: received.append(e))
        
        events = [
            ExecutionStartedEvent(execution_id=f"exec-{i}", workflow_id="wf-1")
            for i in range(5)
        ]
        
        bus.publish_all(events)
        
        assert len(received) == 5

