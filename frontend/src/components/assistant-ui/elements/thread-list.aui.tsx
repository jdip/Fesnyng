"use client";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { useThreadPins } from "@/ThreadPins";
import { ThreadNotificationBadge } from "@/ThreadNotificationBadge";
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
  Loader2Icon,
  MoreHorizontalIcon,
  PencilIcon,
  PinIcon,
  PlusIcon,
  TrashIcon,
} from "lucide-react";
import {
  forwardRef,
  useEffect,
  useRef,
  useState,
  type ComponentPropsWithoutRef,
  type FC,
} from "react";

export const ThreadList: FC<{ pageSize?: number }> = ({ pageSize = 6 }) => {
  const pins = useThreadPins();
  const [showArchived, setShowArchived] = useState(false);
  const archivedCount = useAuiState((s) => s.threads.archivedThreadIds.length);

  return (
    <ThreadListRoot>
      <ThreadListNew />
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
    </ThreadListRoot>
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
    {visible.length < threadIds.length && <Button variant="ghost" aria-label="Show more threads" onClick={() => setVisibleCount((count) => count + pageSize)}>Show more</Button>}
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
  const pins = useThreadPins();
  const session = useAuiState((s) => s.threadListItem.remoteId);
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

  return (
    <ThreadListItemPrimitive.Root
      data-slot="aui_thread-list-item"
      className="group hover:bg-muted focus-visible:bg-muted data-active:bg-muted has-focus-visible:bg-muted has-data-[state=open]:bg-muted relative flex h-8 items-center rounded-md transition-colors focus-visible:outline-none"
    >
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
          data-slot="aui_thread-list-item-trigger"
          className="focus-visible:ring-ring/50 flex h-full min-w-0 flex-1 items-center rounded-md px-2.5 text-start text-sm outline-none group-hover:pe-9 group-has-focus-visible:pe-9 group-has-data-[state=open]:pe-9 group-data-active:pe-9 focus-visible:ring-1"
        >
          {pinned && <PinIcon role="img" aria-label="Pinned" className="me-1.5 size-3.5 shrink-0" />}
          {session && <ThreadNotificationBadge session={session} />}
          {isRunning && (
            <Loader2Icon
              aria-hidden
              data-slot="aui_thread-list-item-running"
              className="text-muted-foreground me-1.5 size-3.5 shrink-0 animate-spin"
            />
          )}
          <span
            data-slot="aui_thread-list-item-title"
            className="min-w-0 flex-1 truncate"
          >
            <ThreadListItemPrimitive.Title fallback="New Chat" />
          </span>
          {isRunning && <span className="sr-only">Running</span>}
        </ThreadListItemPrimitive.Trigger>
      )}
      <ThreadListItemMore
        archived={isArchived}
        onRename={() => setIsRenaming(true)}
      />
    </ThreadListItemPrimitive.Root>
  );
};

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
}> = ({ archived, onRename }) => {
  const pins = useThreadPins();
  const session = useAuiState((s) => s.threadListItem.remoteId);
  const pinned = !!session && !!pins?.ids?.has(session);
  return (
    <ThreadListItemMorePrimitive.Root sharedFocusGroup>
      <ThreadListItemMorePrimitive.Trigger asChild>
        <Button
          variant="ghost"
          size="icon"
          data-slot="aui_thread-list-item-more"
          className="data-[state=open]:bg-accent absolute end-1.5 top-1/2 size-6 -translate-y-1/2 p-0 opacity-0 group-hover:opacity-100 group-has-focus-visible:opacity-100 group-data-active:opacity-100 data-[state=open]:opacity-100"
        >
          <MoreHorizontalIcon className="size-3.5" />
          <span className="sr-only">More options</span>
        </Button>
      </ThreadListItemMorePrimitive.Trigger>
      <ThreadListItemMorePrimitive.Content
        side="right"
        align="start"
        sideOffset={6}
        data-slot="aui_thread-list-item-more-content"
        className="bg-popover text-popover-foreground data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95 data-[state=open]:animate-in data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[state=closed]:animate-out data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 z-50 min-w-32 overflow-hidden rounded-xl border p-1.5"
      >
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
        <ThreadListItemPrimitive.Delete asChild>
          <ThreadListItemMorePrimitive.Item
            data-slot="aui_thread-list-item-more-item"
            className="text-destructive hover:bg-destructive/10 hover:text-destructive focus:bg-destructive/10 focus:text-destructive flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm outline-none select-none"
          >
            <TrashIcon className="size-4" />
            Delete
          </ThreadListItemMorePrimitive.Item>
        </ThreadListItemPrimitive.Delete>
      </ThreadListItemMorePrimitive.Content>
    </ThreadListItemMorePrimitive.Root>
  );
};
