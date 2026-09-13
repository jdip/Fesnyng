import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { NativeQuestion } from './Conversation';

afterEach(cleanup);

test('submits the selected native question choice and never invents an answer', async () => {
  const onReply = vi.fn().mockResolvedValue(undefined);
  render(
    <NativeQuestion
      request={{
        id: 'question-one', sessionID: 'session-one', askedAt: 0, questions: [{
          header: 'Release', question: 'May I deploy?', multiple: false, custom: false,
          options: [
            { label: 'Deploy', description: 'Release now' },
            { label: 'Hold', description: 'Wait for review' },
          ],
        }],
      }}
      onReply={onReply}
      onReject={vi.fn().mockResolvedValue(undefined)}
    />,
  );

  const answer = screen.getByRole('button', { name: 'Answer' });
  expect((answer as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: /Deploy/ }));
  fireEvent.click(answer);

  expect(onReply).toHaveBeenCalledWith([['Deploy']]);
});

test('requires an explicit custom answer before replying', async () => {
  const onReply = vi.fn().mockResolvedValue(undefined);
  render(
    <NativeQuestion
      request={{
        id: 'question-custom', sessionID: 'session-one', askedAt: 0, questions: [{
          header: 'Other', question: 'What should happen?', multiple: false, custom: true,
          options: [],
        }],
      }}
      onReply={onReply}
      onReject={vi.fn().mockResolvedValue(undefined)}
    />,
  );

  const answer = screen.getByRole('button', { name: 'Answer' });
  expect((answer as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByRole('textbox', { name: 'Other answer' }), {
    target: { value: 'Investigate the logs' },
  });
  fireEvent.click(answer);

  expect(onReply).toHaveBeenCalledWith([['Investigate the logs']]);
});

test('offers the native custom answer field when the optional flag is omitted', () => {
  render(
    <NativeQuestion
      request={{
        id: 'question-default-custom', sessionID: 'session-one', askedAt: 0, questions: [{
          header: 'Other', question: 'What should happen?', multiple: false,
          options: [],
        }],
      }}
      onReply={vi.fn().mockResolvedValue(undefined)}
      onReject={vi.fn().mockResolvedValue(undefined)}
    />,
  );

  expect(screen.getByRole('textbox', { name: 'Other answer' })).toBeTruthy();
});

test('keeps the request pending and shows the reply failure', async () => {
  const onReply = vi.fn().mockRejectedValue(new Error('Question has expired'));
  render(
    <NativeQuestion
      request={{
        id: 'question-error', sessionID: 'session-one', askedAt: 0, questions: [{
          header: 'Release', question: 'May I deploy?', multiple: false, custom: false,
          options: [{ label: 'Deploy', description: 'Release now' }],
        }],
      }}
      onReply={onReply}
      onReject={vi.fn().mockResolvedValue(undefined)}
    />,
  );

  fireEvent.click(screen.getByRole('button', { name: /Deploy/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Answer' }));

  expect((await screen.findByRole('alert')).textContent).toContain('Question has expired');
  expect((screen.getByRole('button', { name: 'Answer' }) as HTMLButtonElement).disabled).toBe(false);
});

test('submits an answer only once while the native reply is pending', () => {
  let resolveReply: (() => void) | undefined;
  const onReply = vi.fn(() => new Promise<void>((resolve) => { resolveReply = resolve; }));
  render(
    <NativeQuestion
      request={{
        id: 'question-pending', sessionID: 'session-one', askedAt: 0, questions: [{
          header: 'Release', question: 'May I deploy?', multiple: false, custom: false,
          options: [{ label: 'Deploy', description: 'Release now' }],
        }],
      }}
      onReply={onReply}
      onReject={vi.fn().mockResolvedValue(undefined)}
    />,
  );

  fireEvent.click(screen.getByRole('button', { name: /Deploy/ }));
  const answer = screen.getByRole('button', { name: 'Answer' });
  fireEvent.click(answer);
  fireEvent.click(answer);

  expect(onReply).toHaveBeenCalledTimes(1);
  expect((answer as HTMLButtonElement).disabled).toBe(true);
  resolveReply?.();
});
