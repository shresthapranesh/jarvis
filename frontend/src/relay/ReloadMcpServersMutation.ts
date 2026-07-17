import {graphql} from 'react-relay';
import {commitMutation} from 'react-relay';
import type {ReloadMcpServersMutation} from '../__generated__/ReloadMcpServersMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation ReloadMcpServersMutation {
    reloadMcpServers {
      name
      transport
      toolCount
      enabled
    }
  }
`;

export function commitReloadMcpServers() {
  return new Promise<void>((resolve, reject) => {
    commitMutation<ReloadMcpServersMutation>(environment, {
      mutation,
      variables: {},
      onCompleted: (_res, errors) => {
        if (errors && errors.length) { reject(new Error(errors.map(e => e.message).join('; '))); return; }
        resolve();
      },
      onError: reject,
    });
  });
}
