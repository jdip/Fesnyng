import { useRef, useState } from 'react';
import type { ThreadMenuTarget } from './components/assistant-ui/elements/thread-list.aui';

export type OpenThreadFiles = (target: ThreadMenuTarget, trigger: HTMLElement | null, path?: string) => void;
type FileTarget = ThreadMenuTarget & { path?: string };

/**
 * Keeps the existing workspace-files drawer scoped to the session that opened it.
 * Every launch carries an explicit session, so an active-thread header launch and
 * a non-selected list launch both remain correct if conversation selection changes.
 */
export function useThreadFiles({ onOpened, restoreFocus }: {
  onOpened?: () => void;
  restoreFocus: () => void;
}): { files: FileTarget | undefined; fileFocusRequest: number; openFiles: OpenThreadFiles; closeFiles: () => void } {
  const [panel, setPanel] = useState<FileTarget>();
  const [fileFocusRequest, setFileFocusRequest] = useState(0);
  const trigger = useRef<HTMLElement | null>(null);

  const openFiles: OpenThreadFiles = (target, nextTrigger, path) => {
    trigger.current = nextTrigger;
    setPanel({ ...target, path });
    setFileFocusRequest((current) => current + 1);
    onOpened?.();
  };
  const closeFiles = () => {
    setPanel(undefined);
    trigger.current?.focus();
    if (document.activeElement !== trigger.current) restoreFocus();
  };

  return { files: panel, fileFocusRequest, openFiles, closeFiles };
}
