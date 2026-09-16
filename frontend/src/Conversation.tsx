import {
  AssistantRuntimeProvider,
  type ToolCallMessagePartComponent,
  useAuiState,
} from '@assistant-ui/react';
import {
  useOpenCodeQuestions,
  useOpenCodeRuntime,
  useOpenCodeRuntimeExtras,
  useOpenCodeThreadState,
} from '@assistant-ui/react-opencode';
import { createPortal } from 'react-dom';
import { GitForkIcon } from 'lucide-react';
import { createContext, useContext, useEffect, useMemo, useRef, useState, type PropsWithChildren } from 'react';
import { ThreadPinsProvider } from './ThreadPins';
import { ConversationDeliveryRecovery, ConversationMessageFooter } from './ConversationDelivery';
import { Thread, type ThreadComposerProps, type ThreadGroupPart } from './components/assistant-ui/elements/thread.aui';
import { ThreadList, type ThreadMenuTarget } from './components/assistant-ui/elements/thread-list.aui';
import { ThreadArtifactPanel } from './ThreadArtifact';
import { ThreadPolicyDialog } from './ThreadPolicyDialog';
import { ThreadInformation, type ThreadWorkspace } from './ThreadInformation';
import { ThreadWorkspaceContext, useThreadWorkspace } from './thread-context';
import { NativeEditToolFallback } from './components/assistant-ui/elements/native-edit-tool';
import { NativeQuestionToolFallback } from './components/assistant-ui/elements/native-question-tool';
import {
  ToolGroupContent,
  ToolGroupRoot,
  ToolGroupTrigger,
} from './components/assistant-ui/elements/tool-group.aui';
import { toolPreviewText } from './components/assistant-ui/elements/group-preview';
import { TooltipIconButton } from './components/assistant-ui/elements/tooltip-icon-button';
import { InlineComposer } from './InlineComposer';
import { createFesnyngOpenCodeClient } from './lib/opencode-client';
import { type NativeThreadCreation } from './workspace-api';

export type ConversationProps = {
  /** Absolute `/api/organizations/{org}/agents/{agent}/opencode` facade URL. */
  baseUrl: string;
  /** Browser session CSRF value restored from the control plane. */
  csrfToken: string;
  /** The selected native session, including a restored selection after reload. */
  sessionId?: string;
  /** Receives the runtime's settled session ID after switching or creation. */
  onSessionChange?: (sessionId: string | undefined) => void;
  /** Receives adapter/runtime failures without inventing a conversation reply. */
  onError?: (error: unknown) => void | Promise<void>;
  /** Lets the shell place agent navigation beside or above the runtime thread list. */
  showThreadList?: boolean;
  /** Reloads the maintained thread inventory without remounting the composer. */
  refreshKey?: number;
  threadListTarget?: HTMLElement | null;
  /** Monotonic shell request that starts a maintained native new thread. */
  newThreadRequest?: number;
  /** Control-plane-only selection for one new native thread. */
  creation?: NativeThreadCreation;
  /** Fesnyng-owned grouping labels for employee navigation. */
  projectLabels?: Readonly<Record<string, string>>;
  /** Acknowledges the request only after the runtime accepts its transition. */
  onNewThreadStarted?: (request: number) => void;
  onNewThreadFailed?: (request: number, error: unknown) => void;
  threadPageSize?: number;
  onThreadSelect?: () => void;
  /** A durable historical snapshot. Its original harness remains readable but cannot write. */
  readOnly?: boolean;
  /** A managed workspace is absent or changing; readable history remains mounted. */
  executionBlocked?: boolean;
  executionBlockedState?: 'removed' | 'unavailable' | 'removing' | 'replacing';
  workspace?: ThreadWorkspace;
};

type InlineComposerConfiguration = Pick<ConversationProps, 'baseUrl' | 'csrfToken' | 'sessionId'>;
const InlineComposerConfigurationContext = createContext<InlineComposerConfiguration | undefined>(undefined);

const ConversationComposer = ({ autoFocus, allowAttachments }: ThreadComposerProps) => {
  const configuration = useContext(InlineComposerConfigurationContext);
  if (!configuration) throw new Error('The conversation composer requires its facade configuration.');
  return <InlineComposer {...configuration} autoFocus={autoFocus} allowAttachments={allowAttachments} />;
};

/**
 * An authorized OpenCode conversation for one already-selected Fesnyng agent.
 * Organization and agent navigation remain owned by the application shell.
 */
export function Conversation({
  baseUrl,
  csrfToken,
  sessionId,
  onSessionChange,
  onError,
  showThreadList = true,
  refreshKey = 0,
  threadListTarget,
  newThreadRequest,
  creation,
  projectLabels,
  onNewThreadStarted,
  onNewThreadFailed,
  threadPageSize = 6,
  onThreadSelect,
  readOnly = false,
  executionBlocked = false,
  executionBlockedState,
  workspace,
}: ConversationProps) {
  const [files, setFiles] = useState<ThreadMenuTarget>();
  const [fileFocusRequest, setFileFocusRequest] = useState(0);
  const [permissions, setPermissions] = useState<ThreadMenuTarget>();
  const permissionTrigger = useRef<HTMLButtonElement | null>(null);
  const fileTrigger = useRef<HTMLButtonElement | null>(null);
  const conversationElement = useRef<HTMLElement>(null);
  const openFiles = (target: ThreadMenuTarget, trigger: HTMLButtonElement | null) => {
    fileTrigger.current = trigger;
    setFiles(target);
    setFileFocusRequest((current) => current + 1);
    onThreadSelect?.();
  };
  const closeFiles = () => {
    setFiles(undefined);
    fileTrigger.current?.focus();
    if (document.activeElement !== fileTrigger.current) conversationElement.current?.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message input"]')?.focus();
  };
  const openPermissions = (target: ThreadMenuTarget, trigger: HTMLButtonElement | null) => {
    permissionTrigger.current = trigger;
    setPermissions(target);
    onThreadSelect?.();
  };
  const restorePermissionFocus = () => {
    permissionTrigger.current?.focus();
    if (document.activeElement !== permissionTrigger.current) conversationElement.current?.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message input"]')?.focus();
  };
  const client = useMemo(
    () => creation ? createFesnyngOpenCodeClient(baseUrl, csrfToken, creation) : createFesnyngOpenCodeClient(baseUrl, csrfToken),
    [baseUrl, csrfToken, creation],
  );
  const runtime = useOpenCodeRuntime({
    client,
    initialSessionId: sessionId,
    onThreadIdChange: onSessionChange,
    onError,
  });
  const previousSession = useRef(sessionId);
  useEffect(() => {
    if (previousSession.current === sessionId) return;
    previousSession.current = sessionId;
    if (sessionId && runtime.threads.mainItem.getState().externalId !== sessionId) {
      void runtime.threads.switchToThread(sessionId).catch((error: unknown) => onError?.(error));
    }
  }, [sessionId, runtime, onError]);
  const previousRefresh = useRef(refreshKey);
  useEffect(() => {
    if (previousRefresh.current === refreshKey) return;
    previousRefresh.current = refreshKey;
    void runtime.threads.reload().catch((error: unknown) => onError?.(error));
  }, [refreshKey, runtime, onError]);
  const completedNewThreadRequest = useRef<number | undefined>(undefined);
  const inFlightNewThreadRequest = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (newThreadRequest === undefined || completedNewThreadRequest.current === newThreadRequest || inFlightNewThreadRequest.current === newThreadRequest) return;
    inFlightNewThreadRequest.current = newThreadRequest;
    void runtime.threads.switchToNewThread().then(() => {
      completedNewThreadRequest.current = newThreadRequest;
      onNewThreadStarted?.(newThreadRequest);
    }).catch((error: unknown) => {
      if (onNewThreadFailed) onNewThreadFailed(newThreadRequest, error);
      else onError?.(error);
    }).finally(() => {
      if (inFlightNewThreadRequest.current === newThreadRequest) inFlightNewThreadRequest.current = undefined;
    });
  }, [newThreadRequest, onError, onNewThreadFailed, onNewThreadStarted, runtime]);
  const executionDisabled = readOnly || executionBlocked;
  const components = useMemo(() => ({
    Composer: readOnly ? FrozenThreadNotice : executionBlocked ? () => <WorkspaceUnavailableNotice state={executionBlockedState} /> : ConversationComposer,
    ToolFallback: OpenCodeToolFallback,
    MessageFooter: ConversationMessageFooter,
    ThreadFooter: ConversationDeliveryRecovery,
    ...(executionDisabled ? {} : {
      ToolGroup: PendingApprovalToolGroup,
      MessageAction: () => <OpenCodeForkAction runtime={runtime} onError={onError} />,
    }),
  }), [executionBlocked, executionBlockedState, executionDisabled, onError, readOnly, runtime]);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadWorkspaceProvider baseUrl={baseUrl} csrfToken={csrfToken} refreshKey={refreshKey}>
      <ThreadPinsProvider key={baseUrl} baseUrl={baseUrl} csrfToken={csrfToken} refreshKey={refreshKey} onError={onError}>
      <InlineComposerConfigurationContext.Provider value={{ baseUrl, csrfToken, sessionId }}>
        <section ref={conversationElement} className="fesnyng-conversation" aria-label="Agent conversation">
          {threadListTarget ? createPortal(<ThreadList showNew={false} pageSize={threadPageSize} projectLabels={projectLabels} onSelect={onThreadSelect} onOpenFiles={executionDisabled ? undefined : openFiles} onOpenPermissions={executionDisabled ? undefined : openPermissions} readOnly={executionDisabled} />, threadListTarget) : showThreadList && <aside><ThreadList pageSize={threadPageSize} projectLabels={projectLabels} onSelect={onThreadSelect} onOpenFiles={executionDisabled ? undefined : openFiles} onOpenPermissions={executionDisabled ? undefined : openPermissions} readOnly={executionDisabled} /></aside>}
          <div className="fesnyng-thread-pane"><ActiveThreadInformation runtime={runtime} baseUrl={baseUrl} csrfToken={csrfToken} refreshKey={refreshKey} workspace={workspace} interactionDisabled={executionDisabled} onOpenFiles={executionDisabled ? undefined : openFiles} /><Thread allowAttachments={false} components={components} readOnly={executionDisabled} />{!executionDisabled && <PendingQuestions />}</div>
          {!executionDisabled && files && <ThreadArtifactPanel key={files.id} baseUrl={baseUrl} csrfToken={csrfToken} session={files} focusRequest={fileFocusRequest} onClose={closeFiles} />}
          {!executionDisabled && permissions && <ThreadPolicyDialog key={permissions.id} baseUrl={baseUrl} csrfToken={csrfToken} session={permissions} onClose={() => setPermissions(undefined)} onRestoreFocus={restorePermissionFocus} />}
        </section>
      </InlineComposerConfigurationContext.Provider>
      </ThreadPinsProvider>
      </ThreadWorkspaceProvider>
    </AssistantRuntimeProvider>
  );
}

const FrozenThreadNotice = () => <p className="app-notice" role="status">This thread is permanently frozen and read-only.</p>;
const WorkspaceUnavailableNotice = ({ state }: { state?: ConversationProps['executionBlockedState'] }) => <p className="app-notice" role="status">{state === 'removed' ? 'This workspace was removed. Prepare its replacement before continuing.' : 'This workspace cannot run until its state is resolved. Inspect the workspace before continuing.'}</p>;

function ThreadWorkspaceProvider({ baseUrl, csrfToken, refreshKey, children }: PropsWithChildren<{ baseUrl: string; csrfToken: string; refreshKey: number }>) {
  const revision = useOpenCodeThreadState((state) => [
    state.session?.time?.updated, state.runState.type, Object.keys(state.childSessionsById).join(','),
    state.unhandledEvents.filter((event) => ['session.diff', 'fesnyng.context.updated'].includes(event.type)).at(-1)?.seenAt,
  ].join(':'));
  const value = useMemo(() => ({ baseUrl, csrfToken, refreshKey: `${refreshKey}:${revision}` }), [baseUrl, csrfToken, refreshKey, revision]);
  return <ThreadWorkspaceContext.Provider value={value}>{children}</ThreadWorkspaceContext.Provider>;
}

function ActiveThreadInformation({ runtime, baseUrl, csrfToken, refreshKey, workspace: workspaceDetails, interactionDisabled, onOpenFiles }: {
  runtime: ReturnType<typeof useOpenCodeRuntime>; baseUrl: string; csrfToken: string; refreshKey: number; workspace?: ThreadWorkspace; interactionDisabled: boolean;
  onOpenFiles?: (target: ThreadMenuTarget, trigger: HTMLButtonElement | null) => void;
}) {
  const item = useAuiState((state) => state.threads.threadItems.find((item) => item.id === state.threads.mainThreadId));
  const workspaceContext = useThreadWorkspace();
  const session = item?.externalId ?? item?.remoteId;
  if (!session || !item) return null;
  return <ThreadInformation key={session} baseUrl={baseUrl} csrfToken={csrfToken} session={{ id: session, title: item.title ?? 'New thread' }} refreshKey={workspaceContext?.refreshKey ?? refreshKey} workspace={workspaceDetails} interactionDisabled={interactionDisabled} onOpenFiles={onOpenFiles ? (trigger) => onOpenFiles({ id: session, title: item.title ?? 'New thread' }, trigger) : undefined} onRename={(title) => runtime.threads.getItemById(item.id).rename(title)} />;
}

const OpenCodeToolFallback: ToolCallMessagePartComponent = (props) => {
  const questions = useOpenCodeQuestions();
  const isPendingNativeQuestion = props.status?.type === 'requires-action'
    && questions.some((question) => question.tool?.callID === props.toolCallId);

  return isPendingNativeQuestion
    ? <NativeQuestionToolFallback {...props} />
    : <NativeEditToolFallback {...props} />;
};

/** Opens a maintained tool group only while a real native approval is pending. */
function PendingApprovalToolGroup({
  group,
  children,
}: PropsWithChildren<{ group: ThreadGroupPart }>) {
  const requiresAction = useAuiState((state) => group.indices.some((index) => {
    const part = state.message.parts[index];
    return part?.type === 'tool-call'
      && part.status.type === 'requires-action'
      && part.approval?.approved === undefined
      && part.approval?.resolution === undefined;
  }));
  const latestIndex = group.indices[group.indices.length - 1];
  const preview = useAuiState((state) => {
    const part = latestIndex === undefined ? undefined : state.message.parts[latestIndex];
    return part?.type === 'tool-call' ? toolPreviewText(part.toolName, part.args) : '';
  });
  const [open, setOpen] = useState(false);

  return (
    <ToolGroupRoot variant="ghost" open={requiresAction || open} onOpenChange={setOpen}>
      <ToolGroupTrigger count={group.indices.length} preview={preview} active={group.status.type === 'running'} />
      <ToolGroupContent>{children}</ToolGroupContent>
    </ToolGroupRoot>
  );
}

function OpenCodeForkAction({
  runtime,
  onError,
}: {
  runtime: ReturnType<typeof useOpenCodeRuntime>;
  onError: ConversationProps['onError'];
}) {
  const messageId = useAuiState((state) => state.message.id);
  const { fork } = useOpenCodeRuntimeExtras();
  const [isForking, setIsForking] = useState(false);

  const forkConversation = async () => {
    if (isForking) return;
    setIsForking(true);
    try {
      const sessionId = await fork(messageId);
      await runtime.threads.switchToThread(sessionId);
    } catch (error) {
      await onError?.(error);
    } finally {
      setIsForking(false);
    }
  };

  return (
    <TooltipIconButton
      tooltip="Fork conversation"
      type="button"
      aria-label="Fork conversation"
      disabled={isForking}
      onClick={() => void forkConversation()}
    >
      <GitForkIcon />
    </TooltipIconButton>
  );
}

function PendingQuestions() {
  const questions = useOpenCodeQuestions();

  if (questions.length === 0) return null;
  return <ActivePendingQuestions questions={questions} />;
}

function ActivePendingQuestions({
  questions,
}: {
  questions: ReturnType<typeof useOpenCodeQuestions>;
}) {
  // The adapter creates these actions only after an OpenCode-backed thread is active.
  const { replyToQuestion, rejectQuestion } = useOpenCodeRuntimeExtras();

  return (
    <section className="fesnyng-questions" aria-label="Agent questions">
      {questions.map((request) => (
        <NativeQuestion
          key={request.id}
          request={request}
          onReply={(answers) => replyToQuestion(request.id, answers)}
          onReject={() => rejectQuestion(request.id)}
        />
      ))}
    </section>
  );
}

type NativeQuestionProps = {
  request: ReturnType<typeof useOpenCodeQuestions>[number];
  onReply: (answers: string[][]) => Promise<void>;
  onReject: () => Promise<void>;
};

/** Native questions have no generic assistant-ui component; this narrow slot preserves their real choices. */
export function NativeQuestion({ request, onReply, onReject }: NativeQuestionProps) {
  const [answers, setAnswers] = useState<string[][]>(() => request.questions.map(() => []));
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submissionError, setSubmissionError] = useState<string | undefined>();
  const canReply = answers.every((answer) => answer.length > 0);

  const choose = (questionIndex: number, label: string, multiple: boolean) => {
    setAnswers((current) => current.map((answer, index) => {
      if (index !== questionIndex) return answer;
      if (!multiple) return [label];
      return answer.includes(label) ? answer.filter((value) => value !== label) : [...answer, label];
    }));
  };

  const enterCustomAnswer = (questionIndex: number, value: string) => {
    setAnswers((current) => current.map((answer, index) => {
      if (index !== questionIndex) return answer;
      return value.trim() ? [value] : [];
    }));
  };

  const submit = async (action: () => Promise<void>) => {
    if (isSubmitting) return;
    setIsSubmitting(true);
    setSubmissionError(undefined);
    try {
      await action();
    } catch (error) {
      setSubmissionError(error instanceof Error ? error.message : String(error));
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <article className="fesnyng-question">
      {request.questions.map((question, questionIndex) => (
        <fieldset key={`${request.id}-${question.header}`}>
          <legend>{question.header}</legend>
          <p>{question.question}</p>
          {question.options.map((option) => {
            const selected = answers[questionIndex]?.includes(option.label) ?? false;
            return (
              <button
                key={option.label}
                type="button"
                aria-pressed={selected}
                onClick={() => choose(questionIndex, option.label, question.multiple === true)}
              >
                <strong>{option.label}</strong><span>{option.description}</span>
              </button>
            );
          })}
          {question.custom !== false && (
            <input
              aria-label={`${question.header} answer`}
              value={answers[questionIndex]?.[0] ?? ''}
              onChange={(event) => enterCustomAnswer(questionIndex, event.target.value)}
            />
          )}
        </fieldset>
      ))}
      <div className="fesnyng-question-actions">
        <button type="button" disabled={!canReply || isSubmitting} onClick={() => void submit(() => onReply(answers))}>Answer</button>
        <button type="button" disabled={isSubmitting} onClick={() => void submit(onReject)}>Reject</button>
      </div>
      {submissionError && <p className="fesnyng-question-error" role="alert">{submissionError}</p>}
    </article>
  );
}
