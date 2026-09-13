import type { ToolCallMessagePartComponent } from '@assistant-ui/react';
import {
  ToolFallbackArgs,
  ToolFallbackContent,
  ToolFallbackError,
  ToolFallbackResult,
  ToolFallbackRoot,
  ToolFallbackTrigger,
} from './tool-fallback.aui';

/**
 * OpenCode question requests pause their originating tool call, but answers
 * belong to the native question API. Keep the maintained tool presentation
 * while omitting only its unrelated approval controls.
 */
export const NativeQuestionToolFallback: ToolCallMessagePartComponent = ({
  toolName,
  argsText,
  result,
  status,
}) => {
  const isCancelled = status?.type === 'incomplete' && status.reason === 'cancelled';

  return (
    <ToolFallbackRoot defaultOpen={status?.type === 'requires-action'}>
      <ToolFallbackTrigger toolName={toolName} status={status} />
      <ToolFallbackContent>
        <ToolFallbackError status={status} />
        <ToolFallbackArgs argsText={argsText} />
        {!isCancelled && <ToolFallbackResult result={result} />}
      </ToolFallbackContent>
    </ToolFallbackRoot>
  );
};
