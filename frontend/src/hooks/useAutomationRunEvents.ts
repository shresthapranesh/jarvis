import {graphql} from 'react-relay';

import type {useAutomationRunEventsSubscription} from '../__generated__/useAutomationRunEventsSubscription.graphql';
import {refreshAutomationList} from '../relay/AutomationListQuery';
import {refreshAutomationRuns} from '../relay/AutomationRunsQuery';
import {useRunTokenStream} from './useRunTokenStream';

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

/** Live token stream for one automation run; refetches the run + list on finish. */
export function useAutomationRunEvents(runId: string | null, automationId: string | null) {
  return useRunTokenStream<useAutomationRunEventsSubscription>(
    subscription,
    runId,
    (response) => response.automationRunEvents,
    {
      adoptTerminalOutput: true,
      onFinished: () => {
        void (async () => {
          if (automationId) await refreshAutomationRuns(automationId);
          await refreshAutomationList();
        })();
      },
    },
  );
}
