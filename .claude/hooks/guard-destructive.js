// PreToolUse hook (Bash|PowerShell): blocks recursive force-delete, branch
// deletion, and force-push regardless of flag order/spacing, since Claude's
// permission-rule syntax only matches a literal prefix and can't catch e.g.
// "git push origin --force". Reads the tool call JSON from stdin.
const DENY_PATTERNS = [
  // rm -rf / rm -fr / --recursive --force, any flag order or spacing
  /\brm\s+(?:-[a-z]*\s+)*-?[a-z]*r[a-z]*f[a-z]*\b/i,
  /\brm\s+.*--recursive\b.*--force\b/i,
  /\brm\s+.*--force\b.*--recursive\b/i,
  // PowerShell recursive force delete (Remove-Item / ri / rd / rmdir), any order
  /\b(remove-item|\bri\b|rd|rmdir)\b.*-recurse\b.*-force\b/i,
  /\b(remove-item|\bri\b|rd|rmdir)\b.*-force\b.*-recurse\b/i,
  // git branch delete (force), any order
  /\bgit\s+branch\b.*(-D\b|--delete\s+(--force|-f)\b|-d\s+-f\b|-f\s+-d\b)/i,
  // git push --force / -f / --force-with-lease, anywhere in the command
  /\bgit\s+push\b.*(--force\b|--force-with-lease\b|(?<!\S)-f\b)/i,
];

let input = "";
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", () => {
  let payload;
  try {
    payload = JSON.parse(input);
  } catch {
    process.stdout.write(JSON.stringify({ continue: true }));
    return;
  }

  const command = payload?.tool_input?.command ?? "";
  const hit = DENY_PATTERNS.find((re) => re.test(command));

  if (hit) {
    process.stdout.write(
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName: "PreToolUse",
          permissionDecision: "deny",
          permissionDecisionReason:
            "Blocked by repo policy: destructive/irreversible git or filesystem command (rm -rf, branch delete -D/--force, or git push --force). Run it yourself outside Claude if it's truly needed.",
        },
      }),
    );
    return;
  }

  process.stdout.write(JSON.stringify({ continue: true }));
});
