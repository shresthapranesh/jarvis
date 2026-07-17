import {graphql} from 'react-relay';
import {commitMutation} from 'react-relay';
import type {RemoveMcpServerMutation} from '../__generated__/RemoveMcpServerMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation RemoveMcpServerMutation($name: String!) {
    removeMcpServer(name: $name)
  }
`;

export function commitRemoveMcpServer(name: string) {
  return new Promise<void>((resolve, reject) => {
    commitMutation<RemoveMcpServerMutation>(environment, {
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
