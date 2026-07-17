import {graphql} from 'react-relay';
import {commitMutation} from 'react-relay';
import type {UpdateMcpServerMutation} from '../__generated__/UpdateMcpServerMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation UpdateMcpServerMutation($name: String!, $configJson: String!) {
    updateMcpServer(name: $name, configJson: $configJson) {
      name
      config
      transport
      command
      url
      toolCount
      enabled
    }
  }
`;

export function commitUpdateMcpServer(name: string, configJson: string) {
  return new Promise<void>((resolve, reject) => {
    commitMutation<UpdateMcpServerMutation>(environment, {
      mutation,
      variables: {name, configJson},
      onCompleted: (_res, errors) => {
        if (errors && errors.length) { reject(new Error(errors.map(e => e.message).join('; '))); return; }
        resolve();
      },
      onError: reject,
    });
  });
}
