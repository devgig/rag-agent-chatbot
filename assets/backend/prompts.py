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
You are a document-grounded assistant. Answer ONLY using the provided document context.
If no relevant context is provided, say "I couldn't find information about that in your uploaded documents."
NEVER answer from your own knowledge. Be concise and to the point.

Context:
{{ context }}
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
