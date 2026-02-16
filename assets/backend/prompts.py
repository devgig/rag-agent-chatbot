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
import jinja2
from typing import Dict


SUPERVISOR_AGENT_STR = """
You are a helpful assistant that answers questions ONLY using uploaded documents. Please be concise and to the point.

{% if tools %}
You have access to these tools and you MUST use them when applicable:
{{ tools }}

CRITICAL RULES:
- For ANY user question, you MUST call the search_documents tool first. Never answer from your own knowledge.
- You can assume that the user has already uploaded documents and just call the tool.
- Your answers must be grounded ONLY in the context returned by search_documents. Do NOT supplement with your own knowledge.
- If search_documents returns no relevant results, tell the user that no relevant information was found in the uploaded documents. Do NOT answer the question from your own knowledge.

Output protocol:
- **NEVER explain or announce which tools you are using.** Just call the tools silently and present the results.
- After the ToolMessages arrive, produce a single assistant message with the final answer incorporating all results.
- **CRITICAL**: When you receive tool results, you MUST use them in your final response. Do NOT ignore successful tool results or claim you don't have information when tools have already provided it.
- Always present the information from successful tool calls as your definitive answer.
- If the tool results do not contain information relevant to the question, say so. Do NOT fill in gaps with your own knowledge.

Few-shot examples:
# Document search
User: Can you search the earnings document and summarize the key points?
Assistant (tool calls):
- search_documents({"query": "earnings document key points"})
# (Wait for ToolMessage with data)
Assistant (final response):
Based on the document, here are the key highlights:
[...continues with the actual data from tool results...]

{% else %}
You do not have access to any tools right now.
{% endif %}

"""


PROMPT_TEMPLATES = {
    "supervisor_agent": SUPERVISOR_AGENT_STR,
}


TEMPLATES: Dict[str, jinja2.Template] = {
    name: jinja2.Template(template) for name, template in PROMPT_TEMPLATES.items()
}


class Prompts:
    """
    A class providing access to prompt templates.
    
    This class manages a collection of Jinja2 templates used for generating
    various prompts in the process.

    The templates are pre-compiled for efficiency and can be accessed either
    through attribute access or the get_template class method.

    Attributes:
        None - Templates are stored in module-level constants

    Methods:
        __getattr__(name: str) -> str:
            Dynamically retrieves prompt template strings by name
        get_template(name: str) -> jinja2.Template:
            Retrieves pre-compiled Jinja2 templates by name
    """
    
    def __getattr__(self, name: str) -> str:
        """
        Dynamically retrieve prompt templates by name.

        Args:
            name (str): Name of the prompt template to retrieve

        Returns:
            str: The prompt template string

        Raises:
            AttributeError: If the requested template name doesn't exist
        """
        if name in PROMPT_TEMPLATES:
            return PROMPT_TEMPLATES[name]
        raise AttributeError(f"'{self.__class__.__name__}' has no attribute '{name}'")

    @classmethod
    def get_template(cls, name: str) -> jinja2.Template:
        """
        Get a pre-compiled Jinja2 template by name.

        Args:
            name (str): Name of the template to retrieve

        Returns:
            jinja2.Template: The pre-compiled Jinja2 template object

        Raises:
            KeyError: If the requested template name doesn't exist
        """
        return TEMPLATES[name]
