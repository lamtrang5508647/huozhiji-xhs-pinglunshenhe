"""Public API; importing it does not open a browser or connect to a bot."""
from .api import audit_workbook, convert_table, review

__version__ = "0.2.0"
__all__ = ["audit_workbook", "convert_table", "review", "__version__"]
