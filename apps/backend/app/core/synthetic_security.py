"""
Synthetic check security validation.

This module provides AST-based validation to block obviously dangerous operations
in synthetic check scripts. It is NOT a complete sandbox - synthetic checks execute
arbitrary code and should only be used in trusted, self-hosted deployments.

Security Model:
- Designed for self-hosted, single-organization deployments
- Admin creating synthetic check has equivalent trust to Docker access on agent host
- AST validation blocks obvious attacks but can be bypassed by determined attackers
- NOT suitable for multi-tenant SaaS without additional isolation (Kubernetes pods)

Risk Acceptance (v1.0):
  LuxSwirl v1.0 synthetic checks are designed for self-hosted, single-organization
  deployments where administrators are trusted users. The admin creating a synthetic
  check has equivalent trust to having Docker access on the agent host.

  This feature is NOT suitable for multi-tenant SaaS deployments without additional
  isolation (see v2.0 roadmap with Kubernetes pod isolation).
"""

import ast


class SyntheticSecurityError(Exception):
    """Raised when synthetic check script fails security validation."""


class DangerousOperationVisitor(ast.NodeVisitor):
    """AST visitor that detects dangerous operations in Python code."""

    # Functions that allow arbitrary code execution
    BLOCKED_FUNCTIONS = frozenset(
        [
            "eval",
            "exec",
            "compile",
            "__import__",
            "open",  # File system access
            "input",  # Interactive input
            "breakpoint",  # Debugging
            "globals",  # Global namespace access
            "locals",  # Local namespace access
            "vars",  # Variable inspection
            "dir",  # Object inspection
            "getattr",  # Attribute access
            "setattr",  # Attribute modification
            "delattr",  # Attribute deletion
            "hasattr",  # Attribute existence check (can be used for exploration)
        ]
    )

    # Modules that provide dangerous capabilities
    BLOCKED_MODULES = frozenset(
        [
            "os",  # Operating system access
            "subprocess",  # Process execution
            "socket",  # Network access (beyond Playwright)
            "sys",  # System access
            "importlib",  # Dynamic imports
            "pty",  # Pseudo-terminal
            "shutil",  # File operations
            "pathlib",  # File system access
            "pickle",  # Arbitrary code execution via deserialization
            "marshal",  # Arbitrary code execution
            "shelve",  # Pickle-based storage
            "multiprocessing",  # Process spawning
            "threading",  # Thread spawning (resources)
            "ctypes",  # C library access
            "fcntl",  # File control (Unix)
            "signal",  # Signal handling
            "resource",  # Resource limits
        ]
    )

    # Allowed safe modules for synthetic checks
    ALLOWED_MODULES = frozenset(
        [
            "time",  # Time utilities
            "re",  # Regular expressions
            "json",  # JSON parsing
            "base64",  # Base64 encoding
            "hashlib",  # Hashing
            "datetime",  # Date/time
            "math",  # Math functions
            "random",  # Random numbers (for testing)
            "uuid",  # UUID generation
            "urllib.parse",  # URL parsing only (not fetching)
            "collections",  # Data structures
            "itertools",  # Iterator utilities
            "functools",  # Function utilities
            "operator",  # Operator functions
            "string",  # String utilities
        ]
    )

    def __init__(self):
        """Initialize visitor."""
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        """Check function calls for dangerous operations."""
        # Check direct function calls
        if isinstance(node.func, ast.Name) and node.func.id in self.BLOCKED_FUNCTIONS:
            self.errors.append(
                f"Blocked function: {node.func.id}() at line {node.lineno}. "
                f"This function allows arbitrary code execution."
            )

        # Check attribute calls that might be dangerous
        if isinstance(node.func, ast.Attribute):
            # Block page.__class__.__something__ patterns (sandbox escape)
            if isinstance(node.func.value, ast.Attribute):
                if node.func.value.attr in ["__class__", "__init__", "__globals__"]:
                    self.errors.append(
                        f"Blocked attribute access: {node.func.value.attr} at line {node.lineno}. "
                        f"This pattern can be used to escape Python sandbox."
                    )

        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        """Check imports for dangerous modules."""
        for alias in node.names:
            module_name = alias.name.split(".")[0]  # Get top-level module
            if module_name in self.BLOCKED_MODULES:
                self.errors.append(
                    f"Blocked module: {alias.name} at line {node.lineno}. "
                    f"This module provides dangerous capabilities (file system, process execution, etc.)."
                )
            elif module_name not in self.ALLOWED_MODULES:
                self.warnings.append(
                    f"Unknown module: {alias.name} at line {node.lineno}. "
                    f"Only standard library modules are allowed. Review carefully."
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Check from-imports for dangerous modules."""
        if node.module:
            module_name = node.module.split(".")[0]  # Get top-level module
            if module_name in self.BLOCKED_MODULES:
                self.errors.append(
                    f"Blocked module: {node.module} at line {node.lineno}. "
                    f"This module provides dangerous capabilities."
                )
            elif module_name not in self.ALLOWED_MODULES:
                self.warnings.append(
                    f"Unknown module: {node.module} at line {node.lineno}. "
                    f"Only standard library modules are allowed. Review carefully."
                )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Check attribute access for sandbox escape patterns."""
        # Block direct access to dangerous dunder attributes
        if node.attr in [
            "__class__",
            "__bases__",
            "__subclasses__",
            "__init__",
            "__globals__",
            "__builtins__",
            "__code__",
            "__dict__",
        ]:
            self.errors.append(
                f"Blocked attribute: {node.attr} at line {node.lineno}. "
                f"This attribute can be used to escape Python sandbox."
            )
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        """Check subscript access for dangerous patterns."""
        # Block __builtins__['something'] patterns
        if isinstance(node.value, ast.Name) and node.value.id == "__builtins__":
            self.errors.append(
                f"Blocked access: __builtins__[...] at line {node.lineno}. "
                f"Direct builtins access is not allowed."
            )
        self.generic_visit(node)


def validate_synthetic_script(script_code: str) -> tuple[bool, list[str], list[str]]:
    """
    Validate synthetic check script for obvious security issues.

    This validation uses AST parsing to detect dangerous operations. It is NOT
    a complete sandbox and can be bypassed by determined attackers. Synthetic
    checks should only be used in trusted, self-hosted environments.

    Args:
        script_code: Python code to validate

    Returns:
        Tuple of (is_valid, errors, warnings)
            - is_valid: True if script passes validation
            - errors: List of blocking security errors
            - warnings: List of non-blocking warnings

    Raises:
        SyntheticSecurityError: If script fails security validation
    """
    # Parse the script
    try:
        tree = ast.parse(script_code)
    except SyntaxError as e:
        return (False, [f"Syntax error: {e}"], [])

    # Visit nodes to check for dangerous operations
    visitor = DangerousOperationVisitor()
    visitor.visit(tree)

    # Check if script defines run_check function
    has_run_check = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "run_check":
                has_run_check = True
                # Check if it's async
                if not isinstance(node, ast.AsyncFunctionDef):
                    visitor.errors.append(
                        "run_check() must be defined as an async function (async def run_check(page):)"
                    )
                # Check if it accepts page parameter
                if len(node.args.args) != 1 or node.args.args[0].arg != "page":
                    visitor.errors.append(
                        "run_check() must accept exactly one parameter named 'page'"
                    )
                break

    if not has_run_check:
        visitor.errors.append(
            "Script must define an async function called 'run_check(page)'. "
            "Example:\n\n"
            "async def run_check(page):\n"
            "    await page.goto('https://example.com')\n"
            "    return {'status': 'success', 'steps': ['Loaded page']}"
        )

    # Return validation results
    is_valid = len(visitor.errors) == 0
    return (is_valid, visitor.errors, visitor.warnings)


def validate_and_raise(script_code: str) -> None:
    """
    Validate synthetic check script and raise exception if invalid.

    Args:
        script_code: Python code to validate

    Raises:
        SyntheticSecurityError: If script fails security validation
    """
    is_valid, errors, warnings = validate_synthetic_script(script_code)

    if not is_valid:
        error_message = "Synthetic check script failed security validation:\n\n"
        for i, error in enumerate(errors, 1):
            error_message += f"{i}. {error}\n"

        if warnings:
            error_message += "\nWarnings (review carefully):\n"
            for i, warning in enumerate(warnings, 1):
                error_message += f"{i}. {warning}\n"

        raise SyntheticSecurityError(error_message)
