import { createContext, useCallback, useContext, useMemo, useRef, type PropsWithChildren } from 'react';

export type DraftWorkflow = { name: string; description: string };
export type DraftDeliveryMode = 'queued' | 'steering';
export type DraftAdmission = {
  key: string;
  id: string;
  sessionId?: string;
  mode: DraftDeliveryMode;
};
export type ConversationDraft = {
  text: string;
  workflow?: DraftWorkflow;
  admissions?: DraftAdmission[];
};

type ConversationDrafts = {
  read: (baseUrl: string, threadKey: string) => ConversationDraft | undefined;
  write: (baseUrl: string, threadKey: string, draft: ConversationDraft) => void;
  migrate: (baseUrl: string, fromThreadKey: string, toThreadKey: string) => ConversationDraft | undefined;
  clearDelivered: (baseUrl: string, threadKey: string, admissionId: string, admissionKey: string) => void;
  initialize: (baseUrl: string, threadKey: string, create: () => Promise<string>) => Promise<string>;
};

const ConversationDraftsContext = createContext<ConversationDrafts | undefined>(undefined);

const draftStorageKey = (baseUrl: string, threadKey: string) => `${baseUrl}\u0000${threadKey}`;
const cloneDraft = (draft: ConversationDraft | undefined) => draft && ({
  ...draft,
  workflow: draft.workflow && { ...draft.workflow },
  admissions: draft.admissions?.map((admission) => ({ ...admission })),
});
const draftKey = (draft: ConversationDraft) => `${draft.workflow?.name ?? ''}\u0000${draft.text}`;

/**
 * Keeps unsent composer state only while a user remains in one organization
 * workspace. Native sessions replace the single stable new-thread key once
 * OpenCode has initialized the thread.
 */
export function ConversationDraftsProvider({
  organization,
  children,
}: PropsWithChildren<{ organization: string }>) {
  const draftsRef = useRef(new Map<string, ConversationDraft>());
  const initializationsRef = useRef(new Map<string, Promise<string>>());

  const read = useCallback((baseUrl: string, threadKey: string) => {
    const draft = draftsRef.current.get(draftStorageKey(baseUrl, threadKey));
    return cloneDraft(draft);
  }, []);

  const write = useCallback((baseUrl: string, threadKey: string, draft: ConversationDraft) => {
    const key = draftStorageKey(baseUrl, threadKey);
    if (!draft.text && !draft.workflow && !draft.admissions?.length) {
      draftsRef.current.delete(key);
      return;
    }
    draftsRef.current.set(key, {
      ...draft,
      workflow: draft.workflow && { ...draft.workflow },
      admissions: draft.admissions?.map((admission) => ({ ...admission })),
    });
  }, []);

  const migrate = useCallback((baseUrl: string, fromThreadKey: string, toThreadKey: string) => {
    if (fromThreadKey === toThreadKey) return read(baseUrl, toThreadKey);
    const fromKey = draftStorageKey(baseUrl, fromThreadKey);
    const toKey = draftStorageKey(baseUrl, toThreadKey);
    const source = draftsRef.current.get(fromKey);
    const destination = draftsRef.current.get(toKey);
    if (!source) return cloneDraft(destination);
    // The active new thread is the user's latest work. It deliberately wins
    // over a stale cache for a session that has just been initialized.
    draftsRef.current.set(toKey, source);
    draftsRef.current.delete(fromKey);
    return cloneDraft(source);
  }, [read]);

  const clearDelivered = useCallback((baseUrl: string, threadKey: string, admissionId: string, admissionKey: string) => {
    const key = draftStorageKey(baseUrl, threadKey);
    const draft = draftsRef.current.get(key);
    if (!draft) return;
    const admissions = draft.admissions?.filter((admission) => admission.id !== admissionId);
    if (admissions?.length === draft.admissions?.length) return;
    if (draftKey(draft) === admissionKey) {
      draftsRef.current.delete(key);
      return;
    }
    if (!draft.text && !draft.workflow && !admissions?.length) draftsRef.current.delete(key);
    else draftsRef.current.set(key, { ...draft, admissions });
  }, []);

  const initialize = useCallback((baseUrl: string, threadKey: string, create: () => Promise<string>) => {
    const key = draftStorageKey(baseUrl, threadKey);
    const existing = initializationsRef.current.get(key);
    if (existing) return existing;
    const initialization = create().finally(() => initializationsRef.current.delete(key));
    initializationsRef.current.set(key, initialization);
    return initialization;
  }, []);

  // organization is intentionally part of the provider boundary. App keys this
  // provider by organization, so no draft can cross an organization switch.
  const value = useMemo<ConversationDrafts>(() => ({ read, write, migrate, clearDelivered, initialize }), [clearDelivered, initialize, migrate, read, write]);
  return <ConversationDraftsContext.Provider key={organization} value={value}>{children}</ConversationDraftsContext.Provider>;
}

/** Optional so the maintained composer also works in isolated runtime tests. */
export function useConversationDrafts() {
  return useContext(ConversationDraftsContext);
}
