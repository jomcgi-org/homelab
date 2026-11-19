"""Authenticate MCP binding tokens and authorize their resolved bank operations."""

import json
import os
from contextvars import ContextVar
from hmac import compare_digest
from typing import NamedTuple

from hindsight_api.config import get_config
from hindsight_api.extensions.builtin.tenant import ApiKeyTenantExtension
from hindsight_api.extensions.operation_validator import (
    OperationValidatorExtension,
    ValidationResult,
)
from hindsight_api.extensions.tenant import AuthenticationError, TenantContext


class Binding(NamedTuple):
    name: str
    bank: str
    tools: frozenset[str]


def load_bindings():
    bindings = {}
    for name, policy in json.loads(os.environ["MCP_BINDINGS"]).items():
        token = os.environ[name].encode()
        if not token or token in bindings:
            raise ValueError("MCP bindings require distinct non-empty tokens")
        bindings[token] = Binding(name, policy["bank"], frozenset(policy["tools"]))
    return bindings


BINDINGS = load_bindings()
# The MCP transport authenticates, then resolves the bank alias in the same
# request task with a fresh RequestContext that carries only the credential.
_pending_alias_binding = ContextVar("pending_alias_binding", default=None)


def binding_for(context):
    candidate = (context.api_key or "").encode()
    return next(
        (
            policy
            for token, policy in BINDINGS.items()
            if compare_digest(token, candidate)
        ),
        None,
    )


class BindingTenant(ApiKeyTenantExtension):
    async def authenticate_mcp(self, context):
        binding = binding_for(context)
        if binding is None:
            raise AuthenticationError("Invalid MCP token")
        _pending_alias_binding.set(binding.name)
        context.api_key_id = binding.name
        return TenantContext(schema_name=get_config().database_schema)

    async def authenticate(self, context):
        # Hindsight propagates this transport-set ID to engine calls.
        binding = binding_for(context)
        pending = _pending_alias_binding.get()
        if pending is not None:
            _pending_alias_binding.set(None)
            if binding and pending == binding.name and context.api_key_id is None:
                context.api_key_id = pending
        if binding and context.api_key_id == binding.name:
            return TenantContext(schema_name=get_config().database_schema)
        return await super().authenticate(context)


class BindingValidator(OperationValidatorExtension):
    async def filter_mcp_tools(self, bank_id, request_context, tools):
        binding = binding_for(request_context)
        if binding and binding.bank == bank_id:
            return tools & binding.tools
        return frozenset()

    def validate(self, ctx, operation):
        binding = binding_for(ctx.request_context)
        if binding and (ctx.bank_id != binding.bank or operation not in binding.tools):
            return ValidationResult.reject("Operation outside MCP binding")
        return ValidationResult.accept()

    async def validate_recall(self, ctx):
        return self.validate(ctx, "recall")

    async def validate_retain(self, ctx):
        return self.validate(ctx, "retain")

    async def validate_reflect(self, ctx):
        return self.validate(ctx, "reflect")
