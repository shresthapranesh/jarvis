import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {TodoListQuery} from '../__generated__/TodoListQuery.graphql';
import {environment} from './environment';

export const todoListQuery = graphql`
  query TodoListQuery($conversationId: String!) {
    todos(conversationId: $conversationId) {
      text
      status
    }
  }
`;

export function refreshTodoList(conversationId: string) {
  return fetchQuery<TodoListQuery>(
    environment,
    todoListQuery,
    {conversationId},
    {fetchPolicy: 'network-only'},
  )
    .toPromise()
    .catch(() => undefined);
}
