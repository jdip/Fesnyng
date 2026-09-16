import { useRef, useState } from 'react';
import type { ThreadMenuTarget } from './components/assistant-ui/elements/thread-list.aui';

type OpenFiles = (target: ThreadMenuTarget, trigger: HTMLButtonElement | null) => void;

/**
 * Keeps the existing workspace-files drawer scoped to the session that opened it.
 * Every launch carries an explicit session, so an active-thread header launch and
 * a non-selected list launch both remain correct if conversation selection changes.
 */
export function useThreadFiles({ onOpened, restoreFocus }: {
  onOpened?: () => void;
  restoreFocus: () => void;
}): { files: ThreadMenuTarget | undefined; fileFocusRequest: number; openFiles: OpenFiles; closeFiles: () => void } {
  const [panel, setPanel] = useState<ThreadMenuTarget>();
  const [fileFocusRequest, setFileFocusRequest] = useState(0);
  const trigger = useRef<HTMLButtonElement | null>(null);

  const openFiles: OpenFiles = (target, nextTrigger) => {
    trigger.current = nextTrigger;
    setPanel(target);
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
