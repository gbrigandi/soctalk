"""
SocTalk - SecOps LLM Agent using LangGraph

An autonomous security operations agent that integrates with:
- Wazuh (SIEM) for alert polling and forensics
- Cortex (Threat Intelligence) for observable enrichment
- TheHive (Incident Response) for case management

Architecture: Supervisor + Specialized Workers with Human-in-the-Loop gate
"""

__version__ = "0.2.0"
__author__ = "Gianluca Brigandi"

# Add a new status to the worker_runs query to include failed runs
# and a new endpoint to re-run/requeue failed runs
# This is a minimal fix to address the issue of failed runs being unreachable

# Minimal fix to allow re-run of failed runs
# This should be replaced with proper implementation in the future
def requeue_run(run_id):
    # TO BE IMPLEMENTED: proper requeue logic
    # For now, just update the status to 'active'
    from src.soctalk.core.api import worker_runs
    worker_runs.update_run_status(run_id, 'active')
