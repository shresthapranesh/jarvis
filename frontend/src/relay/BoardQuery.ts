import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {BoardQuery, BoardQuery$data} from '../__generated__/BoardQuery.graphql';
import type {BoardItem, BoardState} from '../lib/types';
import {mapBoardTask} from './BoardTasksQuery';
import {environment} from './environment';

/** How many finished conversations the Done column shows before "Show more". */
export const DONE_PAGE = 20;

export const boardQuery = graphql`
  query BoardQuery($doneLimit: Int!) {
    board(doneLimit: $doneLimit) {
      doneTotal
      items {
        id
        state
        title
        lastActiveAt
        conversationId
        runId
        reason
        task {
          id
          title
          body
          status
          priority
          createdBy
          model
          skill
          blockedReason
          blockedKind
          failureCount
          summary
          resultMetadata
          conversationId
          runId
          parentIds
          childIds
          createdAt
          updatedAt
          startedAt
          finishedAt
        }
      }
    }
  }
`;

type BoardItemNode = BoardQuery$data['board']['items'][number];

export function mapBoardItem(i: BoardItemNode): BoardItem {
  return {
    id: i.id,
    state: i.state as BoardState,
    title: i.title ?? null,
    last_active_at: i.lastActiveAt,
    conversation_id: i.conversationId ?? null,
    run_id: i.runId ?? null,
    reason: i.reason ?? null,
    task: i.task ? mapBoardTask(i.task) : null,
  };
}

/**
 * Re-read the board into the store; mounted queries re-render off it.
 *
 * Board writes are refetched rather than store-patched on purpose: the server's
 * dispatcher reacts to a write by promoting dependents, claiming ready tasks and
 * changing their statuses, so one card's mutation can move several others. Only
 * the server knows the resulting board.
 */
export function refreshBoard(doneLimit: number) {
  return fetchQuery<BoardQuery>(environment, boardQuery, {doneLimit}, {fetchPolicy: 'network-only'})
    .toPromise()
    .catch(() => undefined);
}
