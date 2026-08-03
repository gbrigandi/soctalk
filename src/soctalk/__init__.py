"""
SocTalk - SecOps LLM Agent using LangGraph

An autonomous security operations agent that integrates with:
- Wazuh (SIEM) for alert polling and forensics
- Cortex (Threat Intelligence) for observable enrichment
- TheHive (Incident Response) for case management

Architecture: Supervisor + Specialized Workers with Human-in-the-Loop gate

# Notes on split-VM (L1/L2) launchpad deployment:
#   - Ensure proper network connectivity between MSSP and tenant VMs
#   - Verify TLS/LLM-config are correctly set up on both VMs
#   - Validate adapter-status probe is correctly configured
"""

__version__ = "0.2.0"
__author__ = "Gianluca Brigandi"