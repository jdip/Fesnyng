import type { ToolCallMessagePartComponent } from '@assistant-ui/react';
import { ToolFallback } from './tool-fallback.aui';

type EditChange = {
  path: string;
  action: 'added' | 'changed' | 'deleted';
  before?: string;
  after?: string;
};

const EDIT_TOOLS = new Set(['edit', 'write', 'apply_patch']);

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' ? value as Record<string, unknown> : undefined;
}

function readString(value: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const candidate = value[key];
    if (typeof candidate === 'string') return candidate;
  }
  return undefined;
}

function parsePatchChanges(patchText: string): EditChange[] {
  const changes: EditChange[] = [];
  const fileHeader = /^\*\*\* (Update|Add|Delete) File: (.+)$/;
  let current: EditChange | undefined;
  let before: string[] = [];
  let after: string[] = [];

  const finishCurrent = () => {
    if (!current) return;
    if (before.length > 0) current.before = before.join('\n');
    if (after.length > 0) current.after = after.join('\n');
    changes.push(current);
    current = undefined;
    before = [];
    after = [];
  };

  for (const line of patchText.split('\n')) {
    const header = line.match(fileHeader);
    if (header) {
      finishCurrent();
      const action = header[1] === 'Add' ? 'added' : header[1] === 'Delete' ? 'deleted' : 'changed';
      current = { path: header[2], action };
      continue;
    }
    if (!current) continue;
    if (line.startsWith('+')) after.push(line.slice(1));
    else if (line.startsWith('-')) before.push(line.slice(1));
  }
  finishCurrent();
  return changes;
}

function parseEditChanges(toolName: string, argsText: string | undefined): EditChange[] | undefined {
  if (!EDIT_TOOLS.has(toolName) || !argsText) return undefined;
  try {
    const args = asRecord(JSON.parse(argsText));
    if (!args) return undefined;
    if (toolName === 'apply_patch') {
      const patchText = readString(args, ['patchText', 'patch']);
      const changes = patchText ? parsePatchChanges(patchText) : [];
      return changes.length > 0 ? changes : undefined;
    }
    const path = readString(args, ['filePath', 'path', 'file']);
    const before = readString(args, ['oldString', 'oldText', 'before']);
    const after = readString(args, ['newString', 'newText', 'content', 'after']);
    return path && (before !== undefined || after !== undefined)
      ? [{ path, action: before === undefined ? 'added' : after === undefined ? 'deleted' : 'changed', before, after }]
      : undefined;
  } catch {
    return undefined;
  }
}

/**
 * OpenCode's native `edit`, `write`, and `apply_patch` calls carry structured
 * file changes in their prompt args. Every other tool remains on assistant-ui's
 * maintained fallback.
 */
export const NativeEditToolFallback: ToolCallMessagePartComponent = (props) => {
  const changes = parseEditChanges(props.toolName, props.argsText);
  if (props.isError || !changes || props.status?.type !== 'complete') {
    return <ToolFallback {...props} />;
  }

  const summary = changes.length === 1
    ? <>Edited <code>{changes[0].path}</code></>
    : <>Applied patch to {changes.length} files</>;

  return (
    <details className="fesnyng-native-edit-tool">
      <summary>{summary}</summary>
      <div className="fesnyng-native-edit-diff" aria-label="Structured file changes">
        {changes.map((change) => (
          <section key={change.path} aria-label={`${change.action} ${change.path}`}>
            <p><code>{change.path}</code></p>
            {change.before !== undefined && <pre><del>{change.before}</del></pre>}
            {change.after !== undefined && <pre><ins>{change.after}</ins></pre>}
          </section>
        ))}
      </div>
    </details>
  );
};
