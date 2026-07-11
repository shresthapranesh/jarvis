import {useEffect, useState} from 'react';
import {graphql, requestSubscription} from 'react-relay';

import type {useBoardTaskEventsSubscription} from '../__generated__/useBoardTaskEventsSubscription.graphql';
import {environment} from '../relay/environment';

interface State {
  streaming: boolean;
  text: string;
  error: string | null;
}

const subscription = graphql`
  subscription useBoardTaskEventsSubscription($runId: String!) {
    boardTaskEvents(runId: $runId) {
      __typename
      ... on TokenEvent {
        text
        source
      }
      ... on AutomationDoneEvent {
        output
        runId
      }
      ... on AutomationStoppedEvent {
        output
        runId
      }
      ... on ErrorEvent {
        error
      }
    }
  }
`;

/**
 * Live token stream for one board-task run. `onFinished` fires once on any
 * terminal event so the board list can refetch immediately instead of waiting
 * for the next poll.
 */
export function useBoardTaskEvents(runId: string | null, onFinished?: () => void) {
  const [state, setState] = useState<State>({streaming: false, text: '', error: null});

  useEffect(() => {
    if (!runId) return;
    setState({streaming: true, text: '', error: null});

    const disposable = requestSubscription<useBoardTaskEventsSubscription>(environment, {
      subscription,
      variables: {runId},
      onNext: (response) => {
        const evt = response?.boardTaskEvents;
        if (!evt) return;
        switch (evt.__typename) {
          case 'TokenEvent':
            setState((s) => ({...s, text: s.text + evt.text}));
            break;
          case 'AutomationDoneEvent':
          case 'AutomationStoppedEvent':
            setState((s) => ({...s, streaming: false}));
            onFinished?.();
            break;
          case 'ErrorEvent':
            setState((s) => ({...s, streaming: false, error: evt.error}));
            onFinished?.();
            break;
        }
      },
      onError: (err) => setState((s) => ({...s, streaming: false, error: err.message})),
      onCompleted: () => setState((s) => ({...s, streaming: false})),
    });

    return () => disposable.dispose();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  return state;
}
