import { Component, type ReactNode } from 'react';
import { errorMessage } from './workspace-api';
export class ConversationBoundary extends Component<{ children: ReactNode }, { error: string | null; attempt: number }> {
  state = { error: null as string | null, attempt: 0 };
  static getDerivedStateFromError(error: unknown) { return { error: errorMessage(error) }; }
  render() {
    if (this.state.error) return <div className="app-empty"><div><p className="app-error" role="alert">{this.state.error}</p><button className="app-button" onClick={() => this.setState(({ attempt }) => ({ error: null, attempt: attempt + 1 }))}>Reconnect conversation</button></div></div>;
    return <div key={this.state.attempt} style={{ display: 'contents' }}>{this.props.children}</div>;
  }
}
