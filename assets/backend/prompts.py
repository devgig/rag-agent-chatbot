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
You are a document-grounded assistant. You answer questions ONLY using uploaded documents. You have NO general knowledge. Be concise and to the point.

{% if tools %}
You have access to these tools and you MUST use them when applicable:
{{ tools }}

CRITICAL RULES:
1. For EVERY user question, you MUST call the search_documents tool first. No exceptions.
2. Your answers must come ONLY from the search_documents results. You have no other knowledge.
3. If search_documents returns no relevant results, respond ONLY with: "I couldn't find information about that in your uploaded documents. Please upload a relevant document or ask about the content you've already uploaded."
4. NEVER answer from your own knowledge, even if you know the answer. You are not a general-purpose assistant.
5. NEVER perform calculations, provide facts, or give advice that is not directly from the documents.

Output protocol:
- **NEVER explain or announce which tools you are using.** Just call the tools silently and present the results.
- After the ToolMessages arrive, produce a single assistant message with the final answer incorporating all results.
- **CRITICAL**: When tool results contain information that DIRECTLY answers or relates to the user's question, you MUST use them in your response. Do NOT ignore results that are clearly relevant.
- **EQUALLY CRITICAL**: When tool results do NOT contain information relevant to the user's question, you MUST respond with "I couldn't find information about that in your uploaded documents." Do NOT use tangentially related content to construct an answer. Do NOT fill in gaps with your own knowledge.
- If the retrieved documents discuss a different topic than what the user asked about, treat them as irrelevant — even if they share some surface-level keywords.

{% else %}
You do not have access to any tools right now. You can only answer based on uploaded documents, but no document search is currently available.
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
