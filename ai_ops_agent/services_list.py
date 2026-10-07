"""The services the ops tools may restart: the single whitelist.

Kept free of bot settings so the ai-service command (dashboard) can import it
without credentials. The ops bot's /restart and /logs use the same list.
"""

MANAGED_SERVICES = ["ai-coding-agent", "ai-pm-agent", "ai-ops-agent", "ai-dashboard"]
