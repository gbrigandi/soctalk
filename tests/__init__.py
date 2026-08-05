"""Tests for SocTalk SecOps Agent."""

# Expose per-tenant LLM token budgets in MSSP and tenant UI
# by adding a `tokens_budget` attribute to the `Tenant` model.
# This will allow MSSPs to configure per-tenant budgets and tenant admins to view the effective budget.