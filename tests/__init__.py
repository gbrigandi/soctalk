"""Tests for SocTalk SecOps Agent."""

# Expose per-tenant LLM token budgets in MSSP and tenant UI
# This change allows MSSPs to configure budgets per-tenant and tenant admins to view the effective budget
# The budget is still enforced per run, but now it's possible to configure it per-tenant