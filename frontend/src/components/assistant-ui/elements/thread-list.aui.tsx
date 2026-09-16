"use client";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { useThreadPins } from "@/ThreadPins";
import { ThreadNotificationBadge } from "@/ThreadNotificationBadge";
import { repositoryLabel, useThreadContext, useThreadWorkspace } from "@/thread-context";
import { ThreadCard, ThreadCardActions, ThreadCardContent, threadCardTriggerClassName } from "@/components/ThreadCard";
import {
  AuiIf,
  ThreadListItemMorePrimitive,
  ThreadListItemPrimitive,
  ThreadListPrimitive,
  useAui,
  useAuiState,
} from "@assistant-ui/react";
import {
  ArchiveIcon,
  ArchiveRestoreIcon,
  FolderOpenIcon,
  ShieldCheckIcon,
  Loader2Icon,
  MoreHorizontalIcon,
  PencilIcon,
  PinIcon,
  PlusIcon,
  TrashIcon,
} from "lucide-react";
import {
  createContext,
  useContext,
  forwardRef,
  useEffect,
  useRef,
  useState,
  useId,
  type ComponentPropsWithoutRef,
  type FC,
} from "react";

export type ThreadMenuTarget = { id: string; title: string };
type ThreadNavigation = { onSelect?: () => void; onOpenFiles?: (session: ThreadMenuTarget, trigger: HTMLButtonElement | null) => void; onOpenPermissions?: (session: ThreadMenuTarget, trigger: HTMLButtonElement | null) => void; allowDelete?: boolean; readOnly?: boolean; projectLabels?: Readonly<Record<string, string>> };
const ThreadNavigationContext = createContext<ThreadNavigation>({});

export const ThreadList: FC<ThreadNavigation & { pageSize?: number; showNew?: boolean }> = ({ pageSize = 6, showNew = true, onSelect, onOpenFiles, onOpenPermissions, allowDelete = true, readOnly = false, projectLabels }) => {
  const pins = useThreadPins();
  const [showArchived, setShowArchived] = useState(false);
  const archivedCount = useAuiState((s) => s.threads.archivedThreadIds.length);

  return (
    <ThreadNavigationContext.Provider value={{ onSelect, onOpenFiles, onOpenPermissions, allowDelete, readOnly, projectLabels }}><ThreadListRoot>
      {showNew && !readOnly && <ThreadListNew onClick={onSelect} />}
      {pins?.error && <div role="alert" className="text-sm px-2.5 py-1">{pins.error} <button type="button" onClick={() => { void pins.refresh(); }}>Retry pins</button></div>}
      <ThreadListItems key={pageSize} pageSize={pageSize} />
      {archivedCount > 0 && (
        <>
          <Button
            variant="ghost"
            data-slot="aui_thread-list-archived-toggle"
            className="hover:bg-muted h-8 justify-start gap-2 rounded-md px-2.5 text-sm font-normal"
            aria-expanded={showArchived}
            onClick={() => setShowArchived((visible) => !visible)}
          >
            <ArchiveIcon className="size-4" />
            Archived ({archivedCount})
          </Button>
          {showArchived && <ArchivedThreadListItems />}
        </>
      )}
    </ThreadListRoot></ThreadNavigationContext.Provider>
  );
};

/** Uses the maintained archived collection and item primitive, including restore. */
const ArchivedThreadListItems: FC = () => {
  return (
    <div data-slot="aui_thread-list-archived-items" className="flex flex-col gap-0.5">
      <ThreadListPrimitive.Items archived components={{ ThreadListItem }} />
    </div>
  );
};

export const ThreadListRoot: FC<
  ComponentPropsWithoutRef<typeof ThreadListPrimitive.Root>
> = ({ className, ...props }) => {
  return (
    <ThreadListPrimitive.Root
      data-slot="aui_thread-list-root"
      className={cn("flex flex-col gap-0.5", className)}
      {...props}
    />
  );
};

/** Keeps the maintained item/actions in server order while limiting only unpinned rows. */
export const ThreadListItems: FC<{ pageSize?: number }> = ({ pageSize = 6 }) => {
  const pins = useThreadPins();
  const [visibleCount, setVisibleCount] = useState(pageSize);
  const threadIds = useAuiState((state) => state.threads.threadIds);
  const threadItems = useAuiState((state) => state.threads.threadItems);
  const selected = useAuiState((state) => state.threads.mainThreadId);
  const items = new Map(threadItems.map((item) => [item.id, item]));
  const isPinned = (id: string) => {
    const item = items.get(id);
    return pins?.ids?.has(item?.externalId ?? item?.remoteId ?? '') ?? false;
  };
  const unpinnedPage = new Set(threadIds.filter((id) => !isPinned(id)).slice(0, visibleCount));
  const visible = threadIds.map((id, index) => ({ id, index }))
    .filter(({ id }) => isPinned(id) || unpinnedPage.has(id) || id === selected);
  return <div data-slot="aui_thread-list-items" className="flex flex-col gap-0.5">
    <AuiIf condition={(state) => state.threads.isLoading && state.threads.threadIds.length === 0}><ThreadListSkeleton /></AuiIf>
    {visible.map(({ id, index }) => <ThreadListPrimitive.ItemByIndex key={id} index={index} components={{ ThreadListItem }} />)}
    {visible.length < threadIds.length && <Button variant="ghost" data-slot="aui_thread-list-show-more" className="h-7 justify-start px-2.5 font-normal text-muted-foreground" aria-label="Show more threads" onClick={() => setVisibleCount((count) => count + pageSize)}>Show more</Button>}
  </div>;
};

export const ThreadListNew = forwardRef<
  HTMLButtonElement,
  ComponentPropsWithoutRef<typeof Button> & { labelClassName?: string }
>(({ className, labelClassName, children, ...props }, ref) => {
  return (
    <ThreadListPrimitive.New asChild>
      <Button
        ref={ref}
        variant="ghost"
        data-slot="aui_thread-list-new"
        className={cn(
          "hover:bg-muted data-active:bg-muted h-8 justify-start gap-2 rounded-md px-2.5 text-sm font-normal",
          className,
        )}
        {...props}
      >
        {children ?? (
          <>
            <PlusIcon
              data-slot="aui_thread-list-new-icon"
              className="size-4 shrink-0"
            />
            <span
              data-slot="aui_thread-list-new-label"
              className={cn("whitespace-nowrap", labelClassName)}
            >
              New Thread
            </span>
          </>
        )}
      </Button>
    </ThreadListPrimitive.New>
  );
});

ThreadListNew.displayName = "ThreadListNew";

const ThreadListSkeleton: FC = () => {
  return (
    <div className="flex flex-col gap-0.5">
      {Array.from({ length: 5 }, (_, i) => (
        <div
          key={i}
          role="status"
          aria-label="Loading threads"
          data-slot="aui_thread-list-skeleton-wrapper"
          className="flex h-8 items-center px-2.5"
        >
          <Skeleton
            data-slot="aui_thread-list-skeleton"
            className="h-3.5 w-full"
          />
        </div>
      ))}
    </div>
  );
};

export const ThreadListItem: FC = () => {
  const { onSelect, allowDelete, readOnly, projectLabels } = useContext(ThreadNavigationContext);
  const pins = useThreadPins();
  const session = useAuiState((s) => s.threadListItem.remoteId);
  const workspace = useThreadWorkspace();
  const subtitleId = useId();
  const pinned = !!session && !!pins?.ids?.has(session);
  const isRunning = useAuiState((s) => s.threadListItem.isRunning);
  const isArchived = useAuiState((s) => s.threadListItem.status === "archived");
  const [isRenaming, setIsRenaming] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef(false);

  useEffect(() => {
    if (isRenaming || !restoreFocusRef.current) return;
    restoreFocusRef.current = false;
    triggerRef.current?.focus();
  }, [isRenaming]);

  const project = session && projectLabels?.[session];
  const subtitle = project ?? (session && workspace ? <ThreadRepositorySubtitle session={session} {...workspace} /> : undefined);

  return (
    <ThreadCard as={ThreadListItemPrimitive.Root} data-slot="aui_thread-list-item">
      {isRenaming ? (
        <ThreadListItemRename
          onDone={(restoreFocus) => {
            restoreFocusRef.current = restoreFocus;
            setIsRenaming(false);
          }}
        />
      ) : (
        <ThreadListItemPrimitive.Trigger
          ref={triggerRef}
          aria-describedby={subtitle !== undefined ? subtitleId : undefined}
          onClick={onSelect}
          data-slot="aui_thread-list-item-trigger"
          className={threadCardTriggerClassName()}
        >
          {pinned && <PinIcon role="img" aria-label="Pinned" className="me-1.5 size-3.5 shrink-0" />}
          {isRunning && (
            <Loader2Icon
              aria-hidden
              data-slot="aui_thread-list-item-running"
              className="text-muted-foreground me-1.5 size-3.5 shrink-0 animate-spin"
            />
          )}
          <ThreadCardContent
            title={<ThreadListItemPrimitive.Title fallback="New Chat" />}
            subtitle={subtitle}
            subtitleId={subtitleId}
            subtitleAriaHidden
          />
          {session && <ThreadNotificationBadge session={session} />}
          {isRunning && <span className="sr-only">Running</span>}
        </ThreadListItemPrimitive.Trigger>
      )}
      {!readOnly && <ThreadCardActions><ThreadListItemMore
        archived={isArchived}
        onRename={() => setIsRenaming(true)}
        allowDelete={allowDelete}
      /></ThreadCardActions>}
    </ThreadCard>
  );
};

function ThreadRepositorySubtitle({ session, baseUrl, csrfToken, refreshKey }: { session: string; baseUrl: string; csrfToken: string; refreshKey: string | number }) {
  const { data, failed } = useThreadContext(baseUrl, csrfToken, session, refreshKey);
  const label = !data && !failed ? 'Loading repository…' : repositoryLabel(data?.repository);
  return <span title={label}>{label}</span>;
}

const ThreadListItemRename: FC<{
  onDone: (restoreFocus: boolean) => void;
}> = ({ onDone }) => {
  const aui = useAui();
  const title = useAuiState((s) => s.threadListItem.title) ?? "";
  const [value, setValue] = useState(title);
  const inputRef = useRef<HTMLInputElement>(null);
  const settledRef = useRef(false);

  useEffect(() => {
    inputRef.current?.select();
  }, []);

  const commit = (restoreFocus: boolean) => {
    if (settledRef.current) return;
    settledRef.current = true;

    const next = value.trim();
    if (!next || next === title) {
      onDone(restoreFocus);
      return;
    }

    // Deferred so a synchronous throw lands on the rejection path too.
    Promise.resolve()
      .then(() => aui.threadListItem.rename(next))
      .then(
        () => onDone(restoreFocus),
        () => {
          settledRef.current = false;
          if (restoreFocus) inputRef.current?.focus();
        },
      );
  };

  const cancel = () => {
    if (settledRef.current) return;
    settledRef.current = true;
    onDone(true);
  };

  return (
    <Input
      ref={inputRef}
      autoFocus
      data-slot="aui_thread-list-item-rename"
      aria-label="Rename thread"
      value={value}
      className="h-7 min-w-0 flex-1 ps-2.5 pe-9 text-sm"
      onChange={(event) => setValue(event.target.value)}
      onBlur={() => commit(false)}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          commit(true);
        } else if (event.key === "Escape") {
          event.preventDefault();
          cancel();
        }
      }}
    />
  );
};

const ThreadListItemMore: FC<{
  archived: boolean;
  onRename: () => void;
  allowDelete?: boolean;
}> = ({ archived, onRename, allowDelete = true }) => {
  const pins = useThreadPins();
  const { onOpenFiles, onOpenPermissions } = useContext(ThreadNavigationContext);
  const session = useAuiState((s) => s.threadListItem.remoteId);
  const title = useAuiState((s) => s.threadListItem.title) ?? 'New thread';
  const menuTrigger = useRef<HTMLButtonElement>(null);
  const openedPanel = useRef(false);
  const pinned = !!session && !!pins?.ids?.has(session);
  return (
    <ThreadListItemMorePrimitive.Root sharedFocusGroup>
      <ThreadListItemMorePrimitive.Trigger asChild>
        <Button
          ref={menuTrigger}
          variant="ghost"
          size="icon"
          data-slot="aui_thread-list-item-more"
          className="data-[state=open]:bg-accent size-6 p-0 opacity-0 group-hover:opacity-100 group-has-focus-visible:opacity-100 group-data-active:opacity-100 data-[state=open]:opacity-100"
        >
          <MoreHorizontalIcon className="size-3.5" />
          <span className="sr-only">More options</span>
        </Button>
      </ThreadListItemMorePrimitive.Trigger>
      <ThreadListItemMorePrimitive.Content
        onCloseAutoFocus={(event) => { if (openedPanel.current) { event.preventDefault(); openedPanel.current = false; } }}
        side="right"
        align="start"
        sideOffset={6}
        data-slot="aui_thread-list-item-more-content"
        className="bg-popover text-popover-foreground data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95 data-[state=open]:animate-in data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[state=closed]:animate-out data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 z-50 min-w-32 overflow-hidden rounded-xl border p-1.5"
      >
        {session && onOpenFiles && <ThreadListItemMorePrimitive.Item
          className="hover:bg-accent focus:bg-accent flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm outline-none"
          onSelect={() => { openedPanel.current = true; onOpenFiles({ id: session, title }, menuTrigger.current); }}
        ><FolderOpenIcon aria-hidden className="size-4" />Files</ThreadListItemMorePrimitive.Item>}
        {session && onOpenPermissions && <ThreadListItemMorePrimitive.Item
          className="hover:bg-accent focus:bg-accent flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm outline-none"
          onSelect={() => { openedPanel.current = true; onOpenPermissions({ id: session, title }, menuTrigger.current); }}
        ><ShieldCheckIcon aria-hidden className="size-4" />Thread permissions</ThreadListItemMorePrimitive.Item>}
        {pins && session && !archived && <ThreadListItemMorePrimitive.Item
          disabled={!pins.ids || pins.saving}
          className="hover:bg-accent focus:bg-accent flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm outline-none"
          onSelect={() => { void pins.setPinned(session, !pinned); }}
        ><PinIcon aria-hidden className="size-4" />{pinned ? 'Unpin thread' : 'Pin thread'}</ThreadListItemMorePrimitive.Item>}
        <ThreadListItemMorePrimitive.Item
          data-slot="aui_thread-list-item-more-item"
          className="hover:bg-accent hover:text-accent-foreground focus:bg-accent focus:text-accent-foreground flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm outline-none select-none"
          onSelect={onRename}
        >
          <PencilIcon className="size-4" />
          Rename
        </ThreadListItemMorePrimitive.Item>
        {archived ? (
          <ThreadListItemPrimitive.Unarchive asChild>
            <ThreadListItemMorePrimitive.Item
              data-slot="aui_thread-list-item-more-item"
              className="hover:bg-accent hover:text-accent-foreground focus:bg-accent focus:text-accent-foreground flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm outline-none select-none"
            >
              <ArchiveRestoreIcon className="size-4" />
              Restore
            </ThreadListItemMorePrimitive.Item>
          </ThreadListItemPrimitive.Unarchive>
        ) : (
          <ThreadListItemPrimitive.Archive asChild>
            <ThreadListItemMorePrimitive.Item
              data-slot="aui_thread-list-item-more-item"
              className="hover:bg-accent hover:text-accent-foreground focus:bg-accent focus:text-accent-foreground flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm outline-none select-none"
            >
              <ArchiveIcon className="size-4" />
              Archive
            </ThreadListItemMorePrimitive.Item>
          </ThreadListItemPrimitive.Archive>
        )}
        {allowDelete && <ThreadListItemPrimitive.Delete asChild>
          <ThreadListItemMorePrimitive.Item
            data-slot="aui_thread-list-item-more-item"
            className="text-destructive hover:bg-destructive/10 hover:text-destructive focus:bg-destructive/10 focus:text-destructive flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm outline-none select-none"
          >
            <TrashIcon className="size-4" />
            Delete
          </ThreadListItemMorePrimitive.Item>
        </ThreadListItemPrimitive.Delete>}
      </ThreadListItemMorePrimitive.Content>
    </ThreadListItemMorePrimitive.Root>
  );
};
