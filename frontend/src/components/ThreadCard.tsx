import { cn } from '@/lib/utils';
import type { ComponentPropsWithoutRef, ElementType, ReactNode } from 'react';

type ThreadCardProps = Omit<ComponentPropsWithoutRef<'div'>, 'children'> & {
  as?: ElementType;
  children: ReactNode;
  className?: string;
  active?: boolean;
  'data-slot'?: string;
};

/**
 * Shared presentation shell for a thread in Project and employee navigation.
 * Each owner supplies its native selection and action adapter.
 */
export function ThreadCard({ as: Root = 'div', active, children, className, 'data-slot': slot, ...props }: ThreadCardProps) {
  const activeProps = active === undefined ? {} : { 'data-active': active ? true : undefined };
  return <Root
    {...props}
    {...activeProps}
    data-slot={slot ?? 'thread-card'}
    data-thread-card=""
    className={cn(
      'group hover:bg-muted focus-visible:bg-muted data-active:bg-muted has-focus-visible:bg-muted has-data-[state=open]:bg-muted relative flex min-h-11 items-center rounded-md transition-colors focus-visible:outline-none',
      className,
    )}
  >{children}</Root>;
}

/** The adapter supplies the right-side reservation required by its action slot. */
export function threadCardTriggerClassName(actionPadding = 'pe-9') {
  return `focus-visible:ring-ring/50 flex h-full min-w-0 flex-1 items-center rounded-md px-2.5 ${actionPadding} text-start text-sm outline-none focus-visible:ring-1`;
}

export function ThreadCardContent({ title, subtitle, subtitleId, subtitleAriaHidden = false }: { title: ReactNode; subtitle?: ReactNode; subtitleId?: string; subtitleAriaHidden?: boolean }) {
  return <span data-slot="thread-card-content" className="min-w-0 flex-1 py-1">
    <span data-slot="thread-card-title" className="block truncate">{title}</span>
    {subtitle !== undefined && <>{' '}<small id={subtitleId} data-slot="thread-card-subtitle" aria-hidden={subtitleAriaHidden || undefined} className="block truncate text-xs text-muted-foreground">{subtitle}</small></>}
  </span>;
}

export function ThreadCardActions({ children, className }: { children: ReactNode; className?: string }) {
  return <span data-slot="thread-card-actions" className={cn('absolute end-1.5 top-1/2 flex -translate-y-1/2 items-center', className)}>{children}</span>;
}
