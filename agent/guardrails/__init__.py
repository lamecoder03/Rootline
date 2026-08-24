# The project's four guardrails, each as its own importable module.
# Exists so every one of them is testable in isolation, before an agent loop exists to use them.
# Order of defence: read-only role (db.py) -> SQL validator -> call budget -> audit log.
