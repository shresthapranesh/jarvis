import {graphql} from 'react-relay';

export const settingsQuery = graphql`
  query SettingsQuery {
    settings {
      id
      key
      value
      updatedAt
      isSet
      label
      description
      managedBy
      kind
      choices
      placeholder
      restartRequired
      known
    }
  }
`;
