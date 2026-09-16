"use client";

import { ChevronDownIcon, type LucideIcon } from "lucide-react";
import { CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";

export const groupPreviewText = (text: string) => text.replace(/\s+/g, " ").trim();

export const toolPreviewText = (toolName: string, args: unknown) => {
  const description =
    args &&
    typeof args === "object" &&
    "description" in args &&
    typeof args.description === "string"
      ? groupPreviewText(args.description)
      : "";
  return description || toolName;
};

export function GroupPreviewTrigger({
  icon: Icon,
  label,
  preview,
  active = false,
  className,
  "aria-label": ariaLabel,
  ...props
}: React.ComponentProps<typeof CollapsibleTrigger> & {
  icon: LucideIcon;
  label: string;
  preview: string;
  active?: boolean;
}) {
  const visiblePreview = preview || label;

  return (
    <CollapsibleTrigger
      data-slot="group-preview-trigger"
      aria-label={ariaLabel ?? `${label}: ${visiblePreview}`}
      className={cn(
        "aui-group-preview-trigger group/trigger text-muted-foreground hover:text-foreground flex w-full max-w-full origin-left items-center gap-2 py-1 text-sm transition-[color,scale] active:scale-[0.98]",
        className,
      )}
      {...props}
    >
      <Icon
        data-slot="group-preview-trigger-icon"
        className="aui-group-preview-trigger-icon size-4 shrink-0"
        aria-hidden="true"
      />
      <span
        data-slot="group-preview-trigger-label"
        className={cn(
          "aui-group-preview-trigger-label min-w-0 flex-1 truncate text-start leading-none",
          active && "shimmer motion-reduce:animate-none",
        )}
      >
        {visiblePreview}
      </span>
      <ChevronDownIcon
        data-slot="group-preview-trigger-chevron"
        className={cn(
          "aui-group-preview-trigger-chevron pointer-events-none size-4 shrink-0",
          "transition-transform duration-(--animation-duration) ease-[cubic-bezier(0.32,0.72,0,1)] motion-reduce:transition-none",
          "-rotate-90",
          "group-data-[state=open]/trigger:rotate-0",
        )}
        aria-hidden="true"
      />
    </CollapsibleTrigger>
  );
}
