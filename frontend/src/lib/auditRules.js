/**
 * auditRules.js — what every scanner and every RepoSage rule means.
 *
 * Shared by the audit screen and the PDF report so the two can never drift
 * apart: a rule explained one way on screen and another way in the document a
 * reviewer receives is worse than not explaining it at all.
 */

// ── What each scanner is for ──────────────────────────────────────────────────
export const SCANNERS = [
  { id: 'lint', icon: '🔍', name: 'Linting', tool: 'ruff',
    detects: 'Style violations and the bug-prone patterns a compiler would catch in a typed language: unused names, shadowed builtins, mutable default arguments, comparison and comprehension mistakes.',
    fix: 'Most are mechanical. Run ruff with --fix locally, then review what it could not fix automatically.' },
  { id: 'security', icon: '🔒', name: 'Security', tool: 'bandit',
    detects: 'Known-dangerous constructs in Python itself: shell injection through subprocess, pickle and yaml.load on untrusted input, weak hashes, hardcoded temp paths, disabled certificate verification.',
    fix: 'Treat each as a question about where the input comes from. If any of it is user-controlled, the finding is real.' },
  { id: 'deps', icon: '📦', name: 'Dependency CVEs', tool: 'pip-audit',
    detects: 'Published vulnerabilities in the pinned dependency versions, resolved against the Python Packaging Advisory Database.',
    fix: 'Upgrade to the fixed version named in the advisory. These are the cheapest findings in the report to close.' },
  { id: 'types', icon: '🔬', name: 'Type Analysis', tool: 'mypy',
    detects: 'Type errors provable without running the code: calls that cannot match a signature, attributes that do not exist, None flowing where a value is required.',
    fix: 'Work outward from the most-imported modules; one annotation there usually clears a cluster of downstream errors.' },
  { id: 'secrets', icon: '🔑', name: 'Secret Detection', tool: 'detect-secrets',
    detects: 'Credentials committed to the repository: API keys, private keys, connection strings, and high-entropy strings that look like tokens.',
    fix: 'Rotate first, then remove. A secret that reached a public repository must be considered compromised even after the commit is rewritten.' },
  { id: 'deadcode', icon: '💀', name: 'Dead Code', tool: 'vulture',
    detects: 'Functions, classes, variables and imports with no reference anywhere in the repository.',
    fix: 'Confirm before deleting. Dynamic access by name is invisible to this check, so a plugin entry point can look unused.' },
  { id: 'semgrep', icon: '🧩', name: 'Pattern Analysis', tool: 'semgrep',
    detects: 'Structural anti-patterns matched against a curated community ruleset, including framework-specific mistakes a generic linter has no rules for.',
    fix: 'Each finding links to the rule that produced it; the rule text explains the failure mode it is written for.' },
  { id: 'arch', icon: '🏛️', name: 'Architecture Health', tool: 'RepoSage graph analysis', own: true,
    detects: 'Defects that exist in the SHAPE of the codebase rather than in any one file: import cycles, god modules, functions unreachable over the call graph, dependencies pointing the wrong way.',
    fix: 'These are refactors, not patches. Break the worst cycle first; it usually unlocks several other findings at once.' },
  { id: 'excflow', icon: '⚡', name: 'Exception Flow', tool: 'RepoSage exception propagation', own: true,
    detects: 'Exception-handling defects that need whole-repository flow to see: failures nothing handles, handlers that can never fire, catch-alls that silently swallow distinct deliberate errors, cleanup that destroys the exception already in flight.',
    fix: 'Each finding names the exception type involved. Start from the ones your own code raises on purpose and then discards.' },
]
export const SC = Object.fromEntries(SCANNERS.map(s => [s.id, s]))

// ── Per-rule guidance for RepoSage's own two scanners ─────────────────────────
// The wrapped tools document their own rules; these two are ours, so the report
// has to carry the explanation itself.
export const RULE_INFO = {
  ARCH001: ['Import cycle',
    'Two or more modules import each other, directly or through a chain. None of them can be imported, tested or reused without pulling in the whole group, and import order becomes load-order dependent.',
    'Move the shared names into a module both sides can import, or invert one dependency with a protocol or a callback.'],
  ARCH002: ['God module',
    'A module both imports and is imported by far more modules than the rest of this repository does - more than two standard deviations above its own mean on both axes. A change here can reach most of the codebase.',
    'Split it along the axis its dependents actually use. The usual split is "the types" from "the behaviour".'],
  ARCH003: ['Unreachable function',
    'No path in the call graph arrives at this function from any entry point. This is reachability, not a name search, which is why it finds things a dead-code linter does not.',
    'Confirm before deleting: dynamic dispatch, plugin registration and framework hooks are invisible to static reachability.'],
  ARCH004: ['Unstable dependency',
    'The module imports several others and nothing imports it. Either a script living inside a package, or a layer whose dependencies point the wrong way.',
    'If it is a script, move it out of the package. If it is a layer, invert the dependency.'],
  EXC001: ['Unhandled project exception',
    'The repository defines this exception class and raises it, but no handler anywhere catches it or a base class it defines. Souza et al. measured unhandled exceptions as the largest single root cause of real Python exception-handling bugs.',
    'If escaping is intended - a fail-fast misuse error - document it. If the failure is recoverable, handle it where recovery is possible.'],
  EXC002: ['Dead handler',
    'The handler guards against an exception class this repository defines but never raises or constructs anywhere. The recovery code can never run; usually a refactor removed the raise and left the handler.',
    'Delete the handler, or restore the raise it was written for. Leaving it in place hides the fact that the real failure is unhandled.'],
  EXC003: ['Silent catch-all',
    'A bare or Exception handler with no recovery sits on a path that propagates several distinct exceptions this project raises deliberately. Each one means something specific and all of them are discarded identically.',
    'Catch the specific types you can act on. If the catch-all is a last resort, at minimum log the exception instead of discarding it.'],
  EXC004: ['Exception-replacing cleanup',
    'Code called from this finally block can itself raise. If it does while an exception is already propagating, the cleanup exception replaces it and the original cause disappears from the traceback.',
    'Wrap the cleanup in its own try/except, or use contextlib.suppress for the failures that are genuinely safe to ignore there.'],
  EXC005: ['No common exception root',
    'The package exposes several exception classes with unrelated base classes, so a caller cannot write one except clause that catches everything this package raises - and ends up catching Exception instead.',
    'Give every exception in the package a single shared base class and document it as the one to catch.'],
}

