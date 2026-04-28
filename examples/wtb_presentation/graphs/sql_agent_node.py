"""
SQL Agent Node for WTB Presentation Demo.

Implements a single node that wraps the complete SQL Agent workflow:
    list_tables -> get_schema -> generate_sql -> validate -> execute

This demonstrates wrapping an entire agent graph as a single node
in a larger workflow, enabling:
- Coarse-grained checkpointing (checkpoint after entire SQL flow)
- Simple routing (sql_agent is one node in conditional edges)
- Resource isolation (single venv/Ray allocation for SQL processing)

Architecture:
    ┌────────────────────────────────────────────────────────────────┐
    │                     sql_agent_node (Single Node)                │
    │                                                                 │
    │  Input: state with "sql_query" or "query"                      │
    │                                                                 │
    │  ┌──────────┐   ┌────────────┐   ┌──────────────┐             │
    │  │ list     │ → │ get        │ → │ generate     │             │
    │  │ tables   │   │ schema     │   │ SQL          │             │
    │  └──────────┘   └────────────┘   └──────┬───────┘             │
    │                                          │                      │
    │                                          ▼                      │
    │                               ┌──────────────────┐             │
    │                               │ validate & exec  │             │
    │                               └──────────────────┘             │
    │                                          │                      │
    │  Output: sql_result, tables_schema, execution status           │
    └──────────────────────────────────────────────────────────────────┘

Based on LangChain SQL Agent pattern:
https://docs.langchain.com/oss/python/langgraph/sql-agent
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

WORKSPACE_DIR = Path(__file__).parent.parent / "workspace"
SQL_DIR = WORKSPACE_DIR / "sql"
DEMO_DB_PATH = SQL_DIR / "demo.db"

# LLM Configuration
try:
    from examples.wtb_presentation.config.llm_config import (
        generate_text_result,
        DEFAULT_LLM,
    )
    LLM_AVAILABLE = True
except ImportError:
    LLM_AVAILABLE = False
    DEFAULT_LLM = "gpt-4o-mini"


# ═══════════════════════════════════════════════════════════════════════════════
# SQL Agent Node
# ═══════════════════════════════════════════════════════════════════════════════

def sql_agent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    SQL Agent Node - Wraps complete SQL workflow as single node.
    
    This node processes SQL-related queries by:
    1. Listing available tables in the database
    2. Getting schema information for relevant tables
    3. Generating SQL query from natural language
    4. Validating and executing the query
    5. Returning formatted results
    
    Input state:
        - query: Natural language query (used if sql_query not present)
        - sql_query: Direct SQL query (if already extracted)
        
    Output state:
        - sql_result: Query results as list of dictionaries
        - tables_schema: Schema of accessed tables
        - generated_sql: The SQL that was executed
        - sql_valid: Whether SQL was valid
        - sql_error: Error message if any
        - tables_accessed: List of tables used
        - messages: Updated with SQL agent status
        - status: "completed" or "failed"
        - _output_files: SQL results saved to file
    
    Example:
        state = {"query": "How many rows in the customers table?"}
        result = sql_agent_node(state)
        # result["sql_result"] = [{"count": 59}]
    """
    start_time = time.time()
    messages = []
    
    # Extract query
    query = state.get("sql_query") or state.get("query", "")
    messages.append(f"[sql_agent] Processing query: {query[:50]}...")
    
    try:
        # Step 1: Connect to database and list tables
        db_path = DEMO_DB_PATH
        if not db_path.exists():
            # Create demo database if it doesn't exist
            _create_demo_database(db_path)
        
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # List tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cursor.fetchall()]
        messages.append(f"[sql_agent] Found tables: {', '.join(tables)}")
        
        # Step 2: Get schema for relevant tables
        tables_schema = {}
        relevant_tables = _find_relevant_tables(query, tables)
        
        for table in relevant_tables:
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [{"name": row[1], "type": row[2]} for row in cursor.fetchall()]
            tables_schema[table] = columns
        
        messages.append(f"[sql_agent] Loaded schema for: {', '.join(relevant_tables)}")
        
        # Step 3: Generate SQL from natural language
        generated_sql, llm_cache_meta = _generate_sql_from_query(query, tables_schema)
        messages.append(f"[sql_agent] Generated SQL: {generated_sql}")
        
        # Step 4: Validate SQL
        sql_valid = _validate_sql(generated_sql)
        if not sql_valid:
            raise ValueError(f"Invalid SQL generated: {generated_sql}")
        
        # Step 5: Execute SQL
        cursor.execute(generated_sql)
        rows = cursor.fetchall()
        
        # Convert to list of dicts
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        sql_result = [dict(zip(columns, row)) for row in rows]
        
        conn.close()
        
        duration_ms = (time.time() - start_time) * 1000
        messages.append(f"[sql_agent] Returned {len(sql_result)} rows in {duration_ms:.1f}ms")
        
        # Format answer
        if sql_result:
            answer = f"SQL Query Results:\n\n"
            answer += f"Query: {generated_sql}\n\n"
            answer += f"Results ({len(sql_result)} rows):\n"
            for i, row in enumerate(sql_result[:10]):  # Limit to 10 rows
                answer += f"  {i+1}. {row}\n"
            if len(sql_result) > 10:
                answer += f"  ... and {len(sql_result) - 10} more rows\n"
        else:
            answer = f"SQL Query returned no results.\n\nQuery: {generated_sql}"
        
        # Save results
        output_data = {
            "executed_at": datetime.now().isoformat(),
            "query": query,
            "generated_sql": generated_sql,
            "row_count": len(sql_result),
            "tables_accessed": relevant_tables,
            "results": sql_result[:100],  # Limit saved results
        }
        
        result = {
            "sql_result": sql_result,
            "tables_schema": tables_schema,
            "generated_sql": generated_sql,
            "sql_valid": True,
            "sql_error": None,
            "tables_accessed": relevant_tables,
            "answer": answer,
            "status": "completed",
            "messages": messages,
            "current_node": "sql_agent",
            "_output_files": {"sql_results.json": json.dumps(output_data, indent=2)},
        }
        if llm_cache_meta:
            result["llm_cache_refs"] = {"sql_generate": llm_cache_meta["cache_key"]}
            result["llm_cache_hits"] = {"sql_generate": bool(llm_cache_meta["cache_hit"])}
        return result
        
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        error_msg = str(e)
        messages.append(f"[sql_agent] ERROR: {error_msg} ({duration_ms:.1f}ms)")
        
        return {
            "sql_result": [],
            "tables_schema": {},
            "generated_sql": "",
            "sql_valid": False,
            "sql_error": error_msg,
            "tables_accessed": [],
            "answer": f"SQL Agent Error: {error_msg}",
            "status": "failed",
            "messages": messages,
            "current_node": "sql_agent",
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════

def _find_relevant_tables(query: str, available_tables: List[str]) -> List[str]:
    """
    Find tables relevant to the query.
    
    Simple heuristic: match table names in query text.
    In production, would use LLM to identify relevant tables.
    """
    query_lower = query.lower()
    relevant = []
    
    for table in available_tables:
        # Check if table name or variations appear in query
        table_lower = table.lower()
        if table_lower in query_lower:
            relevant.append(table)
        elif table_lower.rstrip('s') in query_lower:  # Handle plurals
            relevant.append(table)
        elif table_lower + 's' in query_lower:
            relevant.append(table)
    
    # Default to first 3 tables if none found
    return relevant if relevant else available_tables[:3]


def _generate_sql_from_query(
    query: str,
    schema: Dict[str, List[Dict]],
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Generate SQL from natural language query using LLM.
    
    Falls back to rule-based generation if LLM unavailable.
    """
    tables = list(schema.keys())
    
    if not tables:
        return "SELECT 1", None
    
    # Try LLM-based SQL generation
    if LLM_AVAILABLE:
        try:
            schema_str = _format_schema_for_prompt(schema)
            
            prompt = f"""You are a SQL expert. Generate a SQLite query for this request.

Database Schema:
{schema_str}

User Request: {query}

Rules:
1. Only use SELECT statements (no INSERT, UPDATE, DELETE, DROP, etc.)
2. Only use tables and columns from the schema above
3. Include LIMIT clause for safety (max 100 rows)
4. Return ONLY the SQL query, no explanation

SQL Query:"""
            
            response = generate_text_result(
                prompt=prompt,
                model=DEFAULT_LLM,
                temperature=0.1,
                max_tokens=200,
            )
            
            # Extract SQL from response
            sql = _extract_sql_from_response(response.text)
            if sql and _validate_sql(sql):
                return sql, {
                    "cache_key": response.cache_key,
                    "cache_hit": response.cache_hit,
                    "model": response.model,
                }
        except Exception as e:
            print(f"[sql_agent] LLM SQL generation failed: {e}")
    
    # Fallback to rule-based generation
    return _rule_based_sql_generation(query, schema, tables), None


def _format_schema_for_prompt(schema: Dict[str, List[Dict]]) -> str:
    """Format schema for LLM prompt."""
    lines = []
    for table, columns in schema.items():
        cols_str = ", ".join([f"{c['name']} {c['type']}" for c in columns])
        lines.append(f"  {table}: ({cols_str})")
    return "\n".join(lines)


def _extract_sql_from_response(response: str) -> Optional[str]:
    """Extract SQL query from LLM response."""
    response = response.strip()
    
    # Check if response is wrapped in code block
    if "```sql" in response:
        match = re.search(r"```sql\s*(.*?)\s*```", response, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
    elif "```" in response:
        match = re.search(r"```\s*(.*?)\s*```", response, re.DOTALL)
        if match:
            return match.group(1).strip()
    
    # Check if starts with SELECT
    if response.upper().startswith("SELECT"):
        return response.split(";")[0].strip()
    
    return None


def _rule_based_sql_generation(query: str, schema: Dict, tables: List[str]) -> str:
    """Fallback rule-based SQL generation."""
    query_lower = query.lower()
    primary_table = tables[0]
    
    if "count" in query_lower or "how many" in query_lower:
        return f"SELECT COUNT(*) as count FROM {primary_table}"
    
    elif "all" in query_lower or "list" in query_lower or "show" in query_lower:
        return f"SELECT * FROM {primary_table} LIMIT 20"
    
    elif "average" in query_lower or "avg" in query_lower:
        numeric_cols = [c["name"] for c in schema.get(primary_table, []) 
                       if c["type"].upper() in ("INTEGER", "REAL", "NUMERIC")]
        if numeric_cols:
            return f"SELECT AVG({numeric_cols[0]}) as average FROM {primary_table}"
        return f"SELECT COUNT(*) as count FROM {primary_table}"
    
    elif "sum" in query_lower or "total" in query_lower:
        numeric_cols = [c["name"] for c in schema.get(primary_table, []) 
                       if c["type"].upper() in ("INTEGER", "REAL", "NUMERIC")]
        if numeric_cols:
            return f"SELECT SUM({numeric_cols[0]}) as total FROM {primary_table}"
        return f"SELECT COUNT(*) as count FROM {primary_table}"
    
    elif "distinct" in query_lower or "unique" in query_lower:
        cols = schema.get(primary_table, [])
        if cols:
            return f"SELECT DISTINCT {cols[0]['name']} FROM {primary_table} LIMIT 20"
        return f"SELECT DISTINCT * FROM {primary_table} LIMIT 20"
    
    else:
        return f"SELECT * FROM {primary_table} LIMIT 10"


def _validate_sql(sql: str) -> bool:
    """
    Validate SQL query for safety.
    
    Returns True if query is safe to execute.
    """
    sql_upper = sql.upper().strip()
    
    # Only allow SELECT statements
    if not sql_upper.startswith("SELECT"):
        return False
    
    # Block dangerous keywords
    dangerous = ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "CREATE", "TRUNCATE"]
    for kw in dangerous:
        if kw in sql_upper:
            return False
    
    return True


def _create_demo_database(db_path: Path) -> None:
    """
    Create demo SQLite database with sample data.
    
    Tables created:
    - customers: Customer information
    - products: Product catalog
    - orders: Order records
    - workflow_executions: WTB execution metadata
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    # Create customers table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE,
            city TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Create products table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT,
            price REAL,
            stock INTEGER DEFAULT 0
        )
    """)
    
    # Create orders table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER,
            product_id INTEGER,
            quantity INTEGER,
            total_price REAL,
            order_date TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (customer_id) REFERENCES customers(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """)
    
    # Create workflow_executions table (WTB metadata)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS workflow_executions (
            id TEXT PRIMARY KEY,
            workflow_name TEXT NOT NULL,
            status TEXT,
            started_at TEXT,
            completed_at TEXT,
            checkpoint_count INTEGER DEFAULT 0
        )
    """)
    
    # Insert sample data
    customers_data = [
        ("Alice Johnson", "alice@example.com", "New York"),
        ("Bob Smith", "bob@example.com", "Los Angeles"),
        ("Carol White", "carol@example.com", "Chicago"),
        ("David Brown", "david@example.com", "Houston"),
        ("Eve Davis", "eve@example.com", "Phoenix"),
    ]
    cursor.executemany(
        "INSERT OR IGNORE INTO customers (name, email, city) VALUES (?, ?, ?)",
        customers_data
    )
    
    products_data = [
        ("LangGraph Pro", "Software", 99.99, 100),
        ("WTB Enterprise", "Software", 199.99, 50),
        ("Ray Cluster License", "Infrastructure", 499.99, 25),
        ("Vector DB Starter", "Database", 149.99, 75),
        ("LLM API Credits", "Service", 49.99, 1000),
    ]
    cursor.executemany(
        "INSERT OR IGNORE INTO products (name, category, price, stock) VALUES (?, ?, ?, ?)",
        products_data
    )
    
    orders_data = [
        (1, 1, 2, 199.98),
        (2, 2, 1, 199.99),
        (3, 3, 1, 499.99),
        (1, 4, 3, 449.97),
        (4, 5, 10, 499.90),
    ]
    cursor.executemany(
        "INSERT OR IGNORE INTO orders (customer_id, product_id, quantity, total_price) VALUES (?, ?, ?, ?)",
        orders_data
    )
    
    conn.commit()
    conn.close()
    
    print(f"[sql_agent] Created demo database at {db_path}")


# ═══════════════════════════════════════════════════════════════════════════════
# Exports
# ═══════════════════════════════════════════════════════════════════════════════

__all__ = [
    "sql_agent_node",
]
