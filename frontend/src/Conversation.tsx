import {
  AssistantRuntimeProvider,
  type ToolCallMessagePartComponent,
  useAuiState,
} from '@assistant-ui/react';
import {
  useOpenCodeQuestions,
  useOpenCodeRuntime,
  useOpenCodeRuntimeExtras,
} from '@assistant-ui/react-opencode';
import { createPortal } from 'react-dom';
import { GitForkIcon } from 'lucide-react';
import { createContext, useContext, useEffect, useMemo, useRef, useState, type PropsWithChildren } from 'react';
import { ThreadPinsProvider } from './ThreadPins';
import { ThreadReadReceipt } from './ThreadReadReceipt';
import { Thread, type ThreadComposerProps, type ThreadGroupPart } from './components/assistant-ui/elements/thread.aui';
import { ThreadList } from './components/assistant-ui/elements/thread-list.aui';
import { NativeEditToolFallback } from './components/assistant-ui/elements/native-edit-tool';
import { NativeQuestionToolFallback } from './components/assistant-ui/elements/native-question-tool';
import {
  ToolGroupContent,
  ToolGroupRoot,
  ToolGroupTrigger,
} from './components/assistant-ui/elements/tool-group.aui';
import { TooltipIconButton } from './components/assistant-ui/elements/tooltip-icon-button';
import { InlineComposer } from './InlineComposer';
import { createFesnyngOpenCodeClient } from './lib/opencode-client';
import './assistant.css';

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
  threadPageSize?: number;
  onThreadSelect?: () => void;
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
  threadPageSize = 6,
  onThreadSelect,
}: ConversationProps) {
  const client = useMemo(
    () => createFesnyngOpenCodeClient(baseUrl, csrfToken),
    [baseUrl, csrfToken],
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
  const components = useMemo(() => ({
    Composer: ConversationComposer,
    ToolFallback: OpenCodeToolFallback,
    ToolGroup: PendingApprovalToolGroup,
    MessageAction: () => <OpenCodeForkAction runtime={runtime} onError={onError} />,
    MessageFooter: ThreadReadReceipt,
  }), [onError, runtime]);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPinsProvider key={baseUrl} baseUrl={baseUrl} csrfToken={csrfToken} refreshKey={refreshKey} onError={onError}>
      <InlineComposerConfigurationContext.Provider value={{ baseUrl, csrfToken, sessionId }}>
        <section className="fesnyng-conversation" aria-label="Agent conversation">
          {threadListTarget ? createPortal(<ThreadList pageSize={threadPageSize} onSelect={onThreadSelect} />, threadListTarget) : showThreadList && <aside><ThreadList pageSize={threadPageSize} onSelect={onThreadSelect} /></aside>}
          <div className="fesnyng-thread-pane"><Thread allowAttachments={false} components={components} /><PendingQuestions /></div>
        </section>
      </InlineComposerConfigurationContext.Provider>
      </ThreadPinsProvider>
    </AssistantRuntimeProvider>
  );
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
  const [open, setOpen] = useState(false);

  return (
    <ToolGroupRoot variant="ghost" open={requiresAction || open} onOpenChange={setOpen}>
      <ToolGroupTrigger count={group.indices.length} active={group.status.type === 'running'} />
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
