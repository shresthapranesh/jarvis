import {commitMutation, graphql} from 'react-relay';

import type {UpdateWorkflowMutation} from '../__generated__/UpdateWorkflowMutation.graphql';
import type {Workflow} from '../lib/types';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';
import {mapWorkflow} from './WorkflowListQuery';

const mutation = graphql`
  mutation UpdateWorkflowMutation($id: ID!, $input: WorkflowUpdateInput!) {
    updateWorkflow(id: $id, input: $input) {
      id
      name
      description
      definition
      notifications
      createdAt
      updatedAt
    }
  }
`;

interface UpdatePayload {
  name?: string | null;
  description?: string | null;
  definition?: string | null;
  notifications?: string | null;
}

export function commitUpdateWorkflow(rawId: string, p: UpdatePayload): Promise<Workflow> {
  return new Promise((resolve, reject) => {
    commitMutation<UpdateWorkflowMutation>(environment, {
      mutation,
      variables: {
        id: encodeGlobalId('Workflow', rawId),
        input: {
          name: p.name ?? null,
          description: p.description ?? null,
          definition: p.definition ?? null,
          notifications: p.notifications ?? null,
        },
      },
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(mapWorkflow(response.updateWorkflow));
      },
      onError: reject,
    });
  });
}
