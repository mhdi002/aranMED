"""Tool registry. Tools are plug-in callables exposed to the core LLM."""
from .base import Tool, ToolContext, ToolResult, registry  # noqa: F401
from . import builtin  # noqa: F401  - registers built-ins
from . import codeswitch_tools  # noqa: F401  - bilingual ASR repair
from . import ehr  # noqa: F401  - registers EHR tools
from . import alerts  # noqa: F401  - registers alerting tools
from . import education  # noqa: F401  - registers education tools
from . import pacs  # noqa: F401  - registers PACS / imaging tools
from . import clinical  # noqa: F401  - registers EHR / interop tools
