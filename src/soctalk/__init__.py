"""
SocTalk - SecOps LLM Agent using LangGraph

An autonomous security operations agent that integrates with:
- Wazuh (SIEM) for alert polling and forensics
- Cortex (Threat Intelligence) for observable enrichment
- TheHive (Incident Response) for case management

Architecture: Supervisor + Specialized Workers with Human-in-the-Loop gate
"""

__version__ = "0.1.0"
__author__ = "Gianluca Brigandi"
