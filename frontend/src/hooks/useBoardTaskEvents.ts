import {graphql} from 'react-relay';

import type {useBoardTaskEventsSubscription} from '../__generated__/useBoardTaskEventsSubscription.graphql';
import {useRunTokenStream} from './useRunTokenStream';

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
  return useRunTokenStream<useBoardTaskEventsSubscription>(
    subscription,
    runId,
    (response) => response.boardTaskEvents,
    {onFinished},
  );
}
