import { ComposerPrimitive, useAui, useAuiState } from '@assistant-ui/react';
import { ArrowUpIcon, ListPlusIcon, SquareIcon } from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import type { ThreadComposerProps } from './components/assistant-ui/elements/thread.aui';
import { type DraftAdmission, type DraftWorkflow, useConversationDrafts } from './ConversationDrafts';
import { createFesnyngOpenCodeFetch } from './lib/opencode-client';
import { errorMessage } from './workspace-api';

type Workflow = DraftWorkflow;
type DeliveryMode = DraftAdmission['mode'];
type InlineComposerProps = ThreadComposerProps & { baseUrl: string; csrfToken: string; sessionId?: string };
type Admission = DraftAdmission;

const workflowInventoryUrl = (baseUrl: string) => `${baseUrl.replace(/\/$/, '')}/command`;
const admissionFor = (draft: { admissions?: Admission[] } | undefined, key: string) => draft?.admissions?.find((admission) => admission.key === key);
const withAdmission = (draft: { admissions?: Admission[] } | undefined, admission: Admission) => [
  ...(draft?.admissions?.filter((candidate) => candidate.id !== admission.id) ?? []),
  admission,
];

async function responseError(response: Response) {
  const body: unknown = await response.json().catch(() => undefined);
  const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : undefined;
  return typeof detail === 'string' ? detail : `Request failed (${response.status}).`;
}

/** A product slot around the maintained assistant-ui composer primitives. */
export function InlineComposer({ autoFocus, allowAttachments, baseUrl, csrfToken, sessionId }: InlineComposerProps) {
  const aui = useAui();
  const drafts = useConversationDrafts();
  const text = useAuiState((state) => state.composer.text);
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const threadIdentity = useAuiState((state) => state.threadListItem.id);
  const nativeSessionId = useAuiState((state) => state.threadListItem.externalId);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [inventoryError, setInventoryError] = useState('');
  const [selectedWorkflow, setSelectedWorkflow] = useState<Workflow>();
  const [submissionError, setSubmissionError] = useState('');
  const [notice, setNotice] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [slashDismissed, setSlashDismissed] = useState(false);
  const submittingRef = useRef(false);
  const auiRef = useRef(aui);
  const admissionRef = useRef<Admission | undefined>(undefined);
  const textRef = useRef(text);
  const selectedWorkflowRef = useRef<Workflow | undefined>(undefined);
  const restoredKeyRef = useRef<string | undefined>(undefined);
  const previousDraftKeyRef = useRef<string | undefined>(undefined);
  const restoredTextRef = useRef('');
  const skipFirstDraftWriteRef = useRef(true);
  const requestGenerationRef = useRef(0);
  const isMountedRef = useRef(true);
  const fetchWithFesnyngAuth = useMemo(() => createFesnyngOpenCodeFetch(csrfToken), [csrfToken]);

  useEffect(() => { auiRef.current = aui; }, [aui]);
  useEffect(() => { textRef.current = text; }, [text]);
  useEffect(() => { selectedWorkflowRef.current = selectedWorkflow; }, [selectedWorkflow]);

  // A new assistant-ui thread has a local ID that changes on a runtime remount.
  // The provider instead uses one stable new-thread slot per agent facade.
  const draftKey = nativeSessionId || sessionId || 'new';
  const admissionKey = (instructions: string, workflow?: Workflow) => `${workflow?.name ?? ''}\u0000${instructions}`;
  const persistDraft = useCallback((nextText: string, nextWorkflow: Workflow | undefined) => {
    if (!drafts) return;
    const existing = drafts.read(baseUrl, draftKey);
    admissionRef.current = admissionFor(existing, admissionKey(nextText, nextWorkflow));
    drafts.write(baseUrl, draftKey, { text: nextText, workflow: nextWorkflow, admissions: existing?.admissions });
  }, [baseUrl, draftKey, drafts]);

  useLayoutEffect(() => {
    if (!drafts) return;
    let restored = drafts?.read(baseUrl, draftKey);
    const previousKey = previousDraftKeyRef.current;
    if (drafts && previousKey === 'new' && draftKey !== 'new') restored = drafts.migrate(baseUrl, previousKey, draftKey);
    previousDraftKeyRef.current = draftKey;
    restoredKeyRef.current = draftKey;
    restoredTextRef.current = restored?.text ?? '';
    skipFirstDraftWriteRef.current = true;
    admissionRef.current = admissionFor(restored, admissionKey(restoredTextRef.current, restored?.workflow));
    textRef.current = restoredTextRef.current;
    selectedWorkflowRef.current = restored?.workflow;
    auiRef.current.composer.setText(restoredTextRef.current);
    setSelectedWorkflow(restored?.workflow);
    return drafts.subscribeDeliverySettlement(baseUrl, draftKey, (settledAdmissionKey) => {
      if (!isMountedRef.current || admissionKey(textRef.current, selectedWorkflowRef.current) !== settledAdmissionKey) return;
      admissionRef.current = undefined;
      textRef.current = '';
      selectedWorkflowRef.current = undefined;
      auiRef.current.composer.setText('');
      setSelectedWorkflow(undefined);
    });
  }, [baseUrl, draftKey, drafts]);

  useEffect(() => {
    if (!drafts || restoredKeyRef.current !== draftKey) return;
    // assistant-ui initially exposes an empty composer. Do not let that first
    // render overwrite a saved draft before setText() has taken effect.
    if (skipFirstDraftWriteRef.current) {
      if (text === restoredTextRef.current) {
        skipFirstDraftWriteRef.current = false;
        return;
      }
      // An empty new composer has no delayed restore to wait for. This is a
      // real first keystroke, so save it instead of leaving the draft frozen.
      if (restoredTextRef.current) return;
      skipFirstDraftWriteRef.current = false;
    }
    persistDraft(text, selectedWorkflow);
  }, [draftKey, drafts, persistDraft, selectedWorkflow, text]);

  useEffect(() => {
    requestGenerationRef.current += 1;
    const controller = new AbortController();
    void fetchWithFesnyngAuth(workflowInventoryUrl(baseUrl), { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(await responseError(response));
        const value: unknown = await response.json();
        if (!Array.isArray(value) || !value.every((workflow) => (
          workflow && typeof workflow === 'object'
          && typeof workflow.name === 'string' && typeof workflow.description === 'string'
        ))) throw new Error('The workflow inventory response is invalid.');
        setWorkflows(value as Workflow[]);
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setInventoryError(`Workflows are unavailable: ${errorMessage(error)}`);
      });
    return () => controller.abort();
  }, [baseUrl, fetchWithFesnyngAuth]);

  useEffect(() => {
    isMountedRef.current = true;
    return () => { isMountedRef.current = false; };
  }, []);

  const slashMatcher = useCallback((value: string, trigger: string, cursor: number) => {
    if (slashDismissed || !value.startsWith(trigger)) return null;
    const query = value.slice(trigger.length, cursor);
    if (/\s/u.test(query)) return null;
    return { query: query || ' ', offset: 0, endOffset: cursor };
  }, [slashDismissed]);

  const workflowAdapter = useMemo(() => ({
    categories: () => [],
    categoryItems: () => [],
    search: (query: string) => {
      const normalized = query.trim().toLocaleLowerCase();
      return workflows
        .filter((workflow) => workflow.name.toLocaleLowerCase().includes(normalized))
        .map((workflow) => ({ id: workflow.name, type: 'workflow', label: workflow.name, description: workflow.description }));
    },
  }), [workflows]);

  const resolveSessionId = async (threadKey: string) => {
    if (nativeSessionId) return nativeSessionId;
    if (sessionId) return sessionId;
    const initialize = async () => {
      const initialized = await aui.threadListItem.initialize();
      return initialized.externalId ?? initialized.remoteId;
    };
    return drafts ? drafts.initialize(baseUrl, threadKey, initialize) : initialize();
  };

  const sameThread = (requestThreadIdentity: string) => aui.threadListItem.getState().id === requestThreadIdentity;
  const isCurrentThread = (requestThreadIdentity: string) => isMountedRef.current && sameThread(requestThreadIdentity);
  const submit = async (requestedMode: DeliveryMode) => {
    if (submittingRef.current) return;
    const instructions = text;
    const workflow = selectedWorkflow;
    if (!instructions.trim() && !workflow) return;
    const requestThreadIdentity = threadIdentity;
    const requestGeneration = requestGenerationRef.current;
    const key = admissionKey(instructions, workflow);
    let requestDraftKey = draftKey;
    submittingRef.current = true;
    setIsSubmitting(true);
    setSubmissionError('');
    setNotice('');
    try {
      let currentAdmission = admissionRef.current ?? admissionFor(drafts?.read(baseUrl, requestDraftKey), key);
      if (!currentAdmission || currentAdmission.key !== key) {
        currentAdmission = { key, id: crypto.randomUUID(), mode: requestedMode };
        admissionRef.current = currentAdmission;
      }
      // Store the admission before initialization so a remount reuses its key
      // and the provider's in-flight initialization rather than creating a
      // second native session.
      const beforeInitialization = drafts?.read(baseUrl, requestDraftKey);
      drafts?.write(baseUrl, requestDraftKey, {
        text: beforeInitialization?.text ?? instructions,
        workflow: beforeInitialization?.workflow ?? workflow,
        admissions: withAdmission(beforeInitialization, currentAdmission),
      });
      const resolvedSessionId = currentAdmission.sessionId ?? await resolveSessionId(requestDraftKey);
      currentAdmission = { ...currentAdmission, sessionId: resolvedSessionId };
      admissionRef.current = currentAdmission;
      if (requestDraftKey === 'new') {
        drafts?.migrate(baseUrl, requestDraftKey, resolvedSessionId);
        requestDraftKey = resolvedSessionId;
      }
      // Initialization is asynchronous. Keep an edit made while it was in
      // flight and attach the original admission beside that newer draft.
      const currentText = isCurrentThread(requestThreadIdentity) ? aui.composer.getState().text : undefined;
      const latest = drafts?.read(baseUrl, requestDraftKey);
      drafts?.write(baseUrl, requestDraftKey, {
        text: currentText ?? latest?.text ?? instructions,
        workflow: isCurrentThread(requestThreadIdentity) ? selectedWorkflowRef.current : latest?.workflow ?? workflow,
        admissions: withAdmission(latest, currentAdmission),
      });
      // A remount can await another runtime's initialization. Attach this
      // still-active maintained thread before posting so its conversation and
      // restored draft use the resolved native session. A user navigation has
      // a different local identity and is deliberately left alone.
      if (isCurrentThread(requestThreadIdentity) && aui.threadListItem.getState().externalId !== resolvedSessionId) {
        await aui.threads.switchToThread(resolvedSessionId);
      }
      const response = await fetchWithFesnyngAuth(`${baseUrl.replace(/\/$/, '')}/session/${encodeURIComponent(resolvedSessionId)}/prompt_async`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': currentAdmission.id },
        body: JSON.stringify({
          parts: instructions.trim() ? [{ type: 'text', text: instructions }] : [],
          command: workflow?.name,
          mode: currentAdmission.mode,
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      drafts?.clearDelivered(baseUrl, requestDraftKey, currentAdmission.id, currentAdmission.key);
      if (requestGeneration === requestGenerationRef.current && admissionRef.current?.id === currentAdmission.id) admissionRef.current = undefined;
      if (requestGeneration === requestGenerationRef.current && isCurrentThread(requestThreadIdentity)) {
        if (aui.composer.getState().text === instructions) aui.composer.setText('');
        setSelectedWorkflow((current) => current === workflow ? undefined : current);
        setNotice(workflow ? `/${workflow.name} sent.` : currentAdmission.mode === 'steering' ? 'Steering sent.' : 'Queued.');
      }
    } catch (error) {
      if (requestGeneration === requestGenerationRef.current && isCurrentThread(requestThreadIdentity)) setSubmissionError(errorMessage(error));
    } finally {
      if (requestGeneration === requestGenerationRef.current) {
        submittingRef.current = false;
        if (isCurrentThread(requestThreadIdentity)) setIsSubmitting(false);
      }
    }
  };

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    void submit(isRunning ? 'steering' : 'queued');
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.nativeEvent.isComposing || event.shiftKey) return;
    if (!slashDismissed && /^\/[^\s]*$/u.test(text)) {
      if (event.key === 'Escape') setSlashDismissed(true);
      return;
    }
    if (isRunning && event.key === 'Enter') {
      event.preventDefault();
      void submit('steering');
    }
  };

  const canSubmit = Boolean(text.trim() || selectedWorkflow) && !isSubmitting;
  return <ComposerPrimitive.Unstable_TriggerPopoverRoot>
    <ComposerPrimitive.Root className="aui-composer-root relative flex w-full flex-col" onSubmit={onSubmit}>
      <div data-slot="aui_composer-shell" className="border-border/60 focus-within:border-border dark:border-muted-foreground/15 dark:focus-within:border-muted-foreground/30 flex w-full cursor-text flex-col gap-2 rounded-(--composer-radius) border bg-(--composer-bg) p-(--composer-padding) transition-[border-color]">
        {selectedWorkflow && <div className="flex items-center gap-2 px-2 text-sm"><span className="rounded bg-muted px-2 py-1">/{selectedWorkflow.description || selectedWorkflow.name}</span><button type="button" className="text-muted-foreground" aria-label={`Remove workflow ${selectedWorkflow.name}`} onClick={() => { setSelectedWorkflow(undefined); persistDraft(text, undefined); }}>Remove</button></div>}
        <ComposerPrimitive.Input placeholder={isRunning ? 'Steer the agent…' : 'Send a message...'} className="aui-composer-input caret-primary placeholder:text-muted-foreground/60 max-h-48 min-h-10 w-full resize-none bg-transparent px-2.5 py-1 text-base leading-6 outline-none" rows={1} autoFocus={autoFocus} enterKeyHint="send" aria-label="Message input" addAttachmentOnPaste={allowAttachments} onChange={(event) => { const nextText = event.target.value; if (!nextText.startsWith('/')) setSlashDismissed(false); skipFirstDraftWriteRef.current = false; persistDraft(nextText, selectedWorkflow); }} onKeyDown={onKeyDown} />
        <ComposerPrimitive.Unstable_TriggerPopover char="/" matcher={slashMatcher} adapter={inventoryError ? undefined : workflowAdapter}>
          <ComposerPrimitive.Unstable_TriggerPopover.Action removeOnExecute onExecute={(item) => {
            const workflow = workflows.find((candidate) => candidate.name === item.id);
            if (workflow) { setSelectedWorkflow(workflow); persistDraft(text, workflow); setSlashDismissed(false); setSubmissionError(''); setNotice(''); }
          }} />
          <ComposerPrimitive.Unstable_TriggerPopoverItems aria-label="Configured workflows">
            {(items) => <div className="mx-2 rounded border bg-card p-1" role="listbox" aria-label="Configured workflows">
              {items.length === 0 ? <p className="px-2 py-1 text-sm text-muted-foreground">No configured workflows match.</p> : items.map((item, index) => <ComposerPrimitive.Unstable_TriggerPopoverItem key={item.id} item={item} index={index} className="block w-full rounded px-2 py-1 text-left text-sm hover:bg-muted aria-selected:bg-muted"><span className="font-medium">/{item.description || item.label}</span></ComposerPrimitive.Unstable_TriggerPopoverItem>)}
            </div>}
          </ComposerPrimitive.Unstable_TriggerPopoverItems>
        </ComposerPrimitive.Unstable_TriggerPopover>
        {text.startsWith('/') && inventoryError && <p className="app-error px-2" role="alert">{inventoryError}</p>}
        <div className="aui-composer-action-wrapper relative flex items-center justify-between px-1">
          {isRunning ? <button type="button" className="flex items-center gap-1 text-sm text-muted-foreground" disabled={!canSubmit} onClick={() => void submit('queued')} aria-label="Queue message"><ListPlusIcon className="size-4" /><span>Queue</span></button> : <span aria-hidden="true" />}
          <div className="flex items-center gap-1.5">
            {!isRunning && !selectedWorkflow && <button type="button" className="aui-composer-send rounded-full bg-primary p-2 text-primary-foreground" disabled={!canSubmit} onClick={() => void submit('queued')} aria-label="Send message"><ArrowUpIcon className="size-4" /></button>}
            {(isRunning || selectedWorkflow) && <button type="button" className="flex items-center gap-1 rounded-full bg-primary px-3 py-2 text-primary-foreground" disabled={!canSubmit} onClick={() => void submit(isRunning ? 'steering' : 'queued')} aria-label={isRunning ? 'Steer agent' : 'Run workflow'}><ArrowUpIcon className="size-4" /><span>{isRunning ? 'Steer' : 'Run'}</span></button>}
            {isRunning && <ComposerPrimitive.Cancel asChild><button type="button" className="rounded-full bg-primary p-2 text-primary-foreground" aria-label="Stop generating"><SquareIcon className="size-4 fill-current" /></button></ComposerPrimitive.Cancel>}
          </div>
        </div>
        {notice && <p className="app-notice px-2" role="status">{notice}</p>}
        {submissionError && <p className="app-error px-2" role="alert">{submissionError}</p>}
      </div>
    </ComposerPrimitive.Root>
  </ComposerPrimitive.Unstable_TriggerPopoverRoot>;
}
