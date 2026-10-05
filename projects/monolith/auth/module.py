"""Private platform account module; token verification remains tier independent."""

from framework import Module


def register(app):
    from auth.platform.router import router

    app.include_router(router)


def register_mcp():
    from auth.platform.mcp import register_mcp_tools

    register_mcp_tools()


MODULE = Module(name="auth", register=register, register_mcp=register_mcp)
