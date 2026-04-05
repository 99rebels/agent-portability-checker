#!/usr/bin/env python3
"""
skill-portabilizer — Audit an agent skill for platform lock-in and cross-agent compatibility.

Usage:
    python3 audit.py <skill_dir>              # audit only, print report
    python3 audit.py <skill_dir> --fix        # auto-fix what we can
    python3 audit.py <skill_dir> --json       # structured JSON output

Checks:
  1. Hardcoded paths (~/.openclaw, /home/, absolute paths in scripts)
  2. Missing SKILL_DATA_DIR support (data dir resolution)
  3. Platform-specific tool dependencies (clawhub, openclaw CLI)
  4. Hardcoded User-Agent strings with platform names
  5. Missing XDG fallback (~/.config/<skill>/)
  6. SKILL.md path references to platform-specific dirs
  7. Setup scripts without --no-browser (headless) support
  8. Credentials that can only be set via file (no env var alt)
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

VERSION = "1.0.0"

# --- Patterns ---

HARDCODED_PATH_PATTERNS = [
    r'~/.openclaw/',
    r'/home/[a-zA-Z0-9_]+/',
    r'/Users/[a-zA-Z0-9_]+/',
]

PLATFORM_CLI_PATTERNS = [
    (r'\bclawhub\b', "clawhub CLI"),
    (r'\bopenclaw\b', "openclaw CLI"),
]

PLATFORM_UA_PATTERN = re.compile(r'["\']User-Agent["\']:\s*["\'][^"\']*openclaw[^"\']*["\']', re.IGNORECASE)
SKILL_DATA_DIR_PATTERN = re.compile(r'SKILL_DATA_DIR')
XDG_FALLBACK_PATTERN = re.compile(r'~/.config/')

CREDENTIAL_FILE_EXTENSIONS = {'.json', '.pem', '.key', '.token', '.env'}
SETUP_SCRIPT_NAMES = {'setup.py', 'setup.sh', 'install.py', 'configure.py'}


def find_files(skill_dir, extensions=None):
    """Recursively find files in skill directory."""
    if extensions is None:
        extensions = {'.py', '.sh', '.js', '.ts', '.md', '.json', '.yaml', '.yml', '.toml'}
    found = []
    for root, dirs, files in os.walk(skill_dir):
        # Skip hidden dirs and node_modules
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules', '__pycache__', 'venv')]
        for f in files:
            if any(f.endswith(ext) for ext in extensions):
                found.append(os.path.join(root, f))
    return found


def is_pattern_definition(content, pos):
    """Check if a match is inside a pattern string literal or fix function (not actual path usage)."""
    before = content[max(0, pos - 200):pos]
    # Heuristic: if near 'HARDCODED_PATH_PATTERNS', 'startswith', '.replace',
    # or inside a regex pattern string, it's not a real hardcoded path.
    indicators = ['HARDCODED_PATH_PATTERNS', 'startswith(', '.replace(', 'r"']
    for ind in indicators:
        if ind in before[-100:]:
            return True
    return False


def read_file(path):
    """Read file contents, return None on error."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except (IOError, OSError):
        return None


def rel_path(skill_dir, file_path):
    """Relative path from skill dir."""
    return os.path.relpath(file_path, skill_dir)


def check_hardcoded_paths(skill_dir, files):
    """Find hardcoded platform-specific paths."""
    issues = []
    for fpath in files:
        content = read_file(fpath)
        if content is None:
            continue
        rel = rel_path(skill_dir, fpath)
        for pattern in HARDCODED_PATH_PATTERNS:
            for match in re.finditer(pattern, content):
                # Skip matches that are pattern definitions, not actual path usage
                if is_pattern_definition(content, match.start()):
                    continue
                line_num = content[:match.start()].count('\n') + 1
                line_text = content.split('\n')[line_num - 1].strip()
                issues.append({
                    "file": rel,
                    "line": line_num,
                    "match": match.group(),
                    "context": line_text[:120],
                    "severity": "error" if fpath.endswith('.py') or fpath.endswith('.sh') else "warn",
                    "auto_fixable": fpath.endswith('.py') or fpath.endswith('.sh') or fpath.endswith('.js'),
                })
    return issues


def check_skill_data_dir(files):
    """Check if scripts use SKILL_DATA_DIR for path resolution."""
    script_files = [f for f in files if f.endswith(('.py', '.sh', '.js'))]
    has_data_dir = False
    files_with_data_dir = []
    for fpath in script_files:
        content = read_file(fpath)
        if content is None:
            continue
        if SKILL_DATA_DIR_PATTERN.search(content):
            has_data_dir = True
            files_with_data_dir.append(rel_path(os.path.dirname(fpath), fpath))
    return {"has_support": has_data_dir, "files": files_with_data_dir}


def check_xdg_fallback(files):
    """Check if scripts have XDG fallback paths."""
    script_files = [f for f in files if f.endswith(('.py', '.sh', '.js'))]
    has_fallback = False
    for fpath in script_files:
        content = read_file(fpath)
        if content is None:
            continue
        if XDG_FALLBACK_PATTERN.search(content):
            has_fallback = True
            break
    return {"has_fallback": has_fallback}


def check_platform_cli(skill_dir, files):
    """Check for platform-specific CLI tool dependencies.
    
    Only flags actual CLI invocations (subprocess, os.system, shell calls),
    not string data keys or variable names.
    """
    issues = []
    script_files = [f for f in files if f.endswith(('.py', '.sh', '.js'))]
    cli_invocation_patterns = [
        r'subprocess\.(run|call|Popen|check_output).*["\']clawhub["\']',
        r'subprocess\.(run|call|Popen|check_output).*["\']openclaw["\']',
        r'os\.system.*["\']clawhub',
        r'os\.system.*["\']openclaw',
        r'\bclawhub\b.*--',  # clawhub with flags (CLI usage)
        r'"clawhub".*\]',  # subprocess list arg like ["clawhub"]
    ]
    for fpath in script_files:
        content = read_file(fpath)
        if content is None:
            continue
        rel = rel_path(skill_dir, fpath)
        for pattern in cli_invocation_patterns:
            for i, line in enumerate(content.split('\n'), 1):
                stripped = line.strip()
                if stripped.startswith('#') or stripped.startswith('//'):
                    continue
                if re.search(pattern, line):
                    tool = "clawhub CLI" if 'clawhub' in pattern else "openclaw CLI"
                    issues.append({
                        "file": rel,
                        "line": i,
                        "tool": tool,
                        "context": stripped[:120],
                        "severity": "warn",
                        "auto_fixable": False,
                    })
    return issues


def check_user_agent(files):
    """Check for platform names in User-Agent strings."""
    issues = []
    script_files = [f for f in files if f.endswith(('.py', '.sh', '.js'))]
    for fpath in script_files:
        content = read_file(fpath)
        if content is None:
            continue
        rel = rel_path(os.path.dirname(fpath), fpath)
        match = PLATFORM_UA_PATTERN.search(content)
        if match:
            line_num = content[:match.start()].count('\n') + 1
            issues.append({
                "file": rel,
                "line": line_num,
                "match": match.group(),
                "severity": "warn",
                "auto_fixable": True,
            })
    return issues


def check_skill_md_paths(skill_dir, files):
    """Check SKILL.md for hardcoded path references."""
    skill_md = os.path.join(skill_dir, "SKILL.md")
    content = read_file(skill_md)
    if content is None:
        return []
    issues = []
    for pattern in HARDCODED_PATH_PATTERNS:
        for match in re.finditer(pattern, content):
            line_num = content[:match.start()].count('\n') + 1
            line_text = content.split('\n')[line_num - 1].strip()
            issues.append({
                "line": line_num,
                "match": match.group(),
                "context": line_text[:120],
                "severity": "warn",
                "auto_fixable": True,
            })
    return issues


def check_headless_setup(skill_dir, files):
    """Check if setup scripts support --no-browser for headless machines."""
    issues = []
    for fpath in files:
        fname = os.path.basename(fpath)
        if fname not in SETUP_SCRIPT_NAMES:
            continue
        content = read_file(fpath)
        if content is None:
            continue
        # Check if it does browser-based auth (run_local_server, webbrowser, open_browser)
        has_browser = any(kw in content for kw in ['run_local_server', 'webbrowser.open', 'open_browser'])
        has_no_browser = '--no-browser' in content or 'no.browser' in content
        if has_browser and not has_no_browser:
            issues.append({
                "file": rel_path(skill_dir, fpath),
                "severity": "info",
                "detail": "Setup script opens a browser but lacks --no-browser flag for headless machines",
                "auto_fixable": False,
            })
    return issues


def check_credential_env_var(skill_dir, files):
    """Check if credentials support env var as alternative to file."""
    script_files = [f for f in files if f.endswith(('.py', '.sh', '.js'))]
    has_file_creds = False
    has_env_var = False
    for fpath in script_files:
        content = read_file(fpath)
        if content is None:
            continue
        if 'os.environ.get' in content or 'getenv' in content:
            # Check if any env var looks like a credential
            for pattern in [r'os\.environ\.get\(["\']([A-Z_]*TOKEN[A-Z_]*)["\']', 
                          r'os\.environ\.get\(["\']([A-Z_]*KEY[A-Z_]*)["\']',
                          r'os\.environ\.get\(["\']([A-Z_]*SECRET[A-Z_]*)["\']',
                          r'os\.environ\.get\(["\']([A-Z_]*API[A-Z_]*)["\']']:
                if re.search(pattern, content):
                    has_env_var = True
                    break
        if 'CREDENTIALS_PATH' in content or 'CREDS_PATH' in content or 'credentials' in content.lower():
            has_file_creds = True
    return {
        "has_file_credentials": has_file_creds,
        "has_env_var_alternative": has_env_var,
        "needs_env_var": has_file_creds and not has_env_var,
    }


def run_audit(skill_dir):
    """Run all checks and return audit results."""
    skill_dir = os.path.abspath(skill_dir)
    if not os.path.isdir(skill_dir):
        print(f"Error: {skill_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    skill_name = os.path.basename(skill_dir)
    files = find_files(skill_dir)

    result = {
        "skill": skill_name,
        "path": skill_dir,
        "version": VERSION,
        "checks": {},
        "summary": {},
    }

    # 1. Hardcoded paths
    result["checks"]["hardcoded_paths"] = check_hardcoded_paths(skill_dir, files)

    # 2. SKILL_DATA_DIR support
    result["checks"]["skill_data_dir"] = check_skill_data_dir(files)

    # 3. XDG fallback
    result["checks"]["xdg_fallback"] = check_xdg_fallback(files)

    # 4. Platform CLI dependencies
    result["checks"]["platform_cli"] = check_platform_cli(skill_dir, files)

    # 5. User-Agent
    result["checks"]["user_agent"] = check_user_agent(files)

    # 6. SKILL.md paths
    result["checks"]["skill_md_paths"] = check_skill_md_paths(skill_dir, files)

    # 7. Headless setup
    result["checks"]["headless_setup"] = check_headless_setup(skill_dir, files)

    # 8. Credential env vars
    result["checks"]["credential_env_vars"] = check_credential_env_var(skill_dir, files)

    # Summary
    errors = sum(1 for check in [
        result["checks"]["hardcoded_paths"],
        result["checks"]["skill_md_paths"],
    ] for item in check if isinstance(item, dict) and item.get("severity") == "error")
    errors += sum(1 for item in result["checks"]["hardcoded_paths"] if isinstance(item, dict) and item.get("severity") == "error")
    errors += sum(1 for item in result["checks"]["skill_md_paths"] if isinstance(item, dict) and item.get("severity") == "error")

    warnings = sum(1 for item in result["checks"]["hardcoded_paths"] if isinstance(item, dict) and item.get("severity") == "warn")
    warnings += sum(1 for item in result["checks"]["platform_cli"] if isinstance(item, dict))
    warnings += sum(1 for item in result["checks"]["user_agent"] if isinstance(item, dict))
    warnings += sum(1 for item in result["checks"]["skill_md_paths"] if isinstance(item, dict) and item.get("severity") == "warn")

    auto_fixable = sum(1 for item in result["checks"]["hardcoded_paths"] if isinstance(item, dict) and item.get("auto_fixable"))
    auto_fixable += sum(1 for item in result["checks"]["user_agent"] if isinstance(item, dict) and item.get("auto_fixable"))
    auto_fixable += sum(1 for item in result["checks"]["skill_md_paths"] if isinstance(item, dict) and item.get("auto_fixable"))

    result["summary"] = {
        "errors": errors,
        "warnings": warnings,
        "auto_fixable": auto_fixable,
        "is_portable": errors == 0 and warnings == 0,
    }

    return result


def format_report(result):
    """Format audit result as human-readable report."""
    lines = []
    s = result["summary"]

    if s["is_portable"]:
        lines.append(f"✅ {result['skill']} — Fully portable")
        lines.append("")
        return "\n".join(lines)

    icon = "❌" if s["errors"] > 0 else "⚠️"
    lines.append(f"{icon} {result['skill']} — {s['errors']} errors, {s['warnings']} warnings ({s['auto_fixable']} auto-fixable)")
    lines.append("")

    # Hardcoded paths
    hp = result["checks"]["hardcoded_paths"]
    if hp:
        lines.append("📍 Hardcoded Paths")
        for item in hp:
            sev = "❌" if item["severity"] == "error" else "⚠️"
            fix = " [auto-fix]" if item["auto_fixable"] else ""
            lines.append(f"  {sev} {item['file']}:{item['line']}: {item['match']}{fix}")
        lines.append("")

    # SKILL_DATA_DIR
    sdd = result["checks"]["skill_data_dir"]
    if not sdd["has_support"] and hp:
        lines.append("🔧 SKILL_DATA_DIR: Not supported — scripts use hardcoded paths")
        lines.append("")

    # XDG fallback
    xdg = result["checks"]["xdg_fallback"]
    if not xdg["has_fallback"] and hp:
        lines.append("🔧 XDG Fallback: Missing ~/.config/ fallback path")
        lines.append("")

    # Platform CLI
    cli = result["checks"]["platform_cli"]
    if cli:
        lines.append("🔌 Platform CLI Dependencies")
        for item in cli:
            lines.append(f"  ⚠️ {item['file']}:{item['line']}: {item['tool']} — {item['context'][:80]}")
        lines.append("  ℹ️  Cannot auto-fix — document as requirement or make optional")
        lines.append("")

    # User-Agent
    ua = result["checks"]["user_agent"]
    if ua:
        lines.append("🏷️  User-Agent Strings")
        for item in ua:
            lines.append(f"  ⚠️ {item['file']}:{item['line']}: {item['match'][:80]} [auto-fix]")
        lines.append("")

    # SKILL.md paths
    md = result["checks"]["skill_md_paths"]
    if md:
        lines.append("📄 SKILL.md Path References")
        for item in md:
            lines.append(f"  ⚠️ Line {item['line']}: {item['match']} [auto-fix]")
        lines.append("")

    # Headless setup
    hs = result["checks"]["headless_setup"]
    if hs:
        lines.append("🖥️  Headless Setup")
        for item in hs:
            lines.append(f"  ℹ️  {item['file']}: {item['detail']}")
        lines.append("")

    # Credential env vars
    cev = result["checks"]["credential_env_vars"]
    if cev.get("needs_env_var"):
        lines.append("🔑 Credential Environment Variables")
        lines.append("  ⚠️ Credentials loaded from file only — no env var alternative")
        lines.append("  ℹ️  Consider supporting GITHUB_TOKEN / GMAIL_TOKEN etc. as env var override")
        lines.append("")

    return "\n".join(lines)


def apply_fixes(result):
    """Auto-fix what we can. Returns list of changes made."""
    skill_dir = result["path"]
    changes = []

    # Fix hardcoded paths in Python scripts
    for item in result["checks"]["hardcoded_paths"]:
        if not item.get("auto_fixable"):
            continue
        fpath = os.path.join(skill_dir, item["file"])
        content = read_file(fpath)
        if content is None:
            continue

        original = content
        old_path = item["match"]

        # Determine replacement
        if old_path.startswith("~/.openclaw/credentials"):
            new_path = "$SKILL_DATA_DIR"
        elif old_path.startswith("~/.openclaw/workspace/data/"):
            # Extract skill-specific part: ~/.openclaw/workspace/data/github-tracker -> github-tracker
            rest = old_path.replace("~/.openclaw/workspace/data/", "")
            new_path = "$SKILL_DATA_DIR/" + rest
        else:
            new_path = "<portable-path>"

        # Replace in content — only exact string matches
        if old_path in content:
            content = content.replace(old_path, new_path, 1)
            changes.append(f"  {item['file']}: {old_path} → {new_path}")

        if content != original:
            with open(fpath, 'w') as f:
                f.write(content)

    return changes


def main():
    parser = argparse.ArgumentParser(description="Audit agent skills for cross-platform portability")
    parser.add_argument("skill_dir", help="Path to skill directory")
    parser.add_argument("--fix", action="store_true", help="Auto-fix what we can")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--version", action="version", version=f"skill-portabilizer {VERSION}")

    args = parser.parse_args()

    result = run_audit(args.skill_dir)

    if args.json:
        print(json.dumps(result, indent=2))
        return

    report = format_report(result)
    print(report)

    if args.fix and result["summary"]["auto_fixable"] > 0:
        print("🔧 Applying auto-fixes...")
        changes = apply_fixes(result)
        if changes:
            print("\nChanges made:")
            for c in changes:
                print(c)
        else:
            print("  No changes needed or all changes already applied.")
        print("\n⚠️  Review changes manually. Some fixes may need adjustment.")
        print("   Re-run without --fix to verify remaining issues.")
    elif args.fix:
        print("✅ Nothing to auto-fix.")


if __name__ == "__main__":
    main()
