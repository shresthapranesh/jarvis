import {useEffect, useState} from 'react';
import {graphql, requestSubscription} from 'react-relay';

import type {useAutomationRunEventsSubscription} from '../__generated__/useAutomationRunEventsSubscription.graphql';
import {refreshAutomationList} from '../relay/AutomationListQuery';
import {refreshAutomationRuns} from '../relay/AutomationRunsQuery';
import {environment} from '../relay/environment';

interface State {
  streaming: boolean;
  text: string;
  error: string | null;
}

const subscription = graphql`
  subscription useAutomationRunEventsSubscription($runId: String!) {
    automationRunEvents(runId: $runId) {
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

export function useAutomationRunEvents(runId: string | null, automationId: string | null) {
  const [state, setState] = useState<State>({streaming: false, text: '', error: null});

  useEffect(() => {
    if (!runId) return;
    setState({streaming: true, text: '', error: null});

    const disposable = requestSubscription<useAutomationRunEventsSubscription>(environment, {
      subscription,
      variables: {runId},
      onNext: (response) => {
        const evt = response?.automationRunEvents;
        if (!evt) return;

        switch (evt.__typename) {
          case 'TokenEvent':
            setState((s) => ({...s, text: s.text + evt.text}));
            break;
          case 'AutomationDoneEvent':
          case 'AutomationStoppedEvent':
            setState((s) => ({
              ...s,
              text: evt.output ?? s.text,
              streaming: false,
            }));
            void (async () => {
              if (automationId) await refreshAutomationRuns(automationId);
              await refreshAutomationList();
            })();
            break;
          case 'ErrorEvent':
            setState((s) => ({...s, streaming: false, error: evt.error}));
            void (async () => {
              if (automationId) await refreshAutomationRuns(automationId);
              await refreshAutomationList();
            })();
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
