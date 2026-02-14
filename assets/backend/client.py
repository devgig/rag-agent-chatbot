#
# SPDX-FileCopyrightText: Copyright (c) 1993-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Multi-Server MCP Client for connecting to multiple MCP servers.

This module provides a unified client interface for connecting to and managing
multiple Model Context Protocol (MCP) servers. It handles server configuration,
initialization, and tool retrieval across different server types.
"""

import os
import sys
from typing import List, Optional

from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp.types import Tool


def get_mcp_env() -> dict:
    """Get environment variables to pass to MCP server subprocesses."""
    env = os.environ.copy()
    # Ensure critical env vars are passed to MCP servers
    for key in ["CONFIG_PATH", "MILVUS_ADDRESS", "MODELS", "POSTGRES_HOST",
                "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"]:
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def get_python_executable() -> str:
    """Get the Python executable path for MCP subprocesses.

    Uses sys.executable to ensure subprocesses use the same Python
    interpreter (with all dependencies) as the main process.
    """
    return sys.executable


class MCPClient:
    """Client for managing connections to multiple MCP servers.

    Provides a unified interface for connecting to and interacting with
    MCP servers including the RAG server.
    """

    def __init__(self):
        """Initialize the MCP client with predefined server configurations."""
        mcp_env = get_mcp_env()
        python_exe = get_python_executable()
        self.server_configs = {
            "rag-server": {
                "command": python_exe,
                "args": ["tools/mcp_servers/rag.py"],
                "transport": "stdio",
                "env": mcp_env,
            }
        }
        self.mcp_client: MultiServerMCPClient | None = None

    async def init(self):
        """Initialize the multi-server MCP client.
        
        Returns:
            MCPClient: Self for method chaining
            
        Raises:
            Exception: If client initialization fails
        """
        self.mcp_client = MultiServerMCPClient(self.server_configs)
        return self

    async def get_tools(self):
        """Retrieve available tools from all connected MCP servers.
        
        Returns:
            List[Tool]: List of available tools from all servers
            
        Raises:
            RuntimeError: If client is not initialized
            Exception: If tool retrieval fails
        """
        if not self.mcp_client:
            raise RuntimeError("MCP client not initialized. Call `await init()` first.")
        
        try:
            tools = await self.mcp_client.get_tools()
            return tools
        except Exception as error:
            print("Error encountered connecting to MCP server. Is the server running? Is your config server path correct?\n")
            raise error