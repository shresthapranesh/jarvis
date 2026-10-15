import {graphql} from 'react-relay';
import {commitMutation} from 'react-relay';
import type {McpLogoutMutation} from '../__generated__/McpLogoutMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation McpLogoutMutation($name: String!) {
    mcpLogout(name: $name)
  }
`;

export function commitMcpLogout(name: string) {
  return new Promise<void>((resolve, reject) => {
    commitMutation<McpLogoutMutation>(environment, {
      mutation,
      variables: {name},
      onCompleted: (_res, errors) => {
        if (errors && errors.length) { reject(new Error(errors.map(e => e.message).join('; '))); return; }
        resolve();
      },
      onError: reject,
    });
  });
}
