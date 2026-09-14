"""The six stage agents.

Each one is a bounded concurrent worker with a typed input and output, not an
autonomous actor: none of them plans, loops on its own judgement, or decides a
number that reaches the report. The orchestrator owns the DAG, the budgets, the
retries and the cancellation, and V1's deterministic layer owns the verdict.

    identity    who is this, confirmed before anything expensive runs
    discovery   which organisations are they connected to
    evidence    what has been published about each one (N+1 instances)
    fulltext    read the articles that look material
    analyst     the only LLM-native agent: what does each article actually say
    network     who do they know, and who should they meet
"""
