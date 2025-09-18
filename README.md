# Employee Onboarding Automation System

A serverless automation system that handles employee onboarding across Active Directory, Microsoft 365, and Atlassian platforms.

## What It Does

This system automates the entire employee onboarding process:
- Creates Active Directory accounts in the correct domain/OU
- Copies permissions from existing users
- Assigns Microsoft 365 licenses
- Creates Jira/Confluence accounts
- Updates tickets with progress in real-time

## Results

- **80% reduction** in manual onboarding time
- **From 2+ hours to 15 minutes** per employee
- **Zero errors** for 600+ users processed
- **5x increase** in daily processing capacity

## Technologies Used

- **AWS Services**: Lambda, SNS, SQS, Systems Manager, Secrets Manager
- **Languages**: Python 3.9, PowerShell
- **APIs**: Microsoft Graph API, Atlassian REST API
- **Infrastructure**: CloudFormation, Cross-account IAM roles

## Repository Structure

```
employee-onboarding-automation/
├── README.md                          # This file
├── lambda_handler.py                  # Main Lambda function code
├── requirements.txt                   # Python dependencies
├── cloudformation/
│   └── template.yaml                 # Infrastructure as code
├── scripts/
│   └── sample_powershell.ps1        # Example AD scripts
├── config/
│   └── sample_ou_mapping.json       # Example OU mapping configuration
└── docs/
    ├── architecture.md               # System architecture
    └── setup_guide.md               # Installation instructions
```

## Architecture Overview

The system uses an event-driven architecture:

1. **Trigger**: Jira ticket status change triggers automation
2. **Processing**: AWS Lambda processes the request
3. **AD Creation**: Cross-account SSM executes PowerShell on domain controllers
4. **Delayed Processing**: SQS handles M365/Atlassian setup after AD sync
5. **Monitoring**: Real-time updates back to Jira ticket

## Key Features

### Multi-Domain Support
- Automatically determines the correct domain based on department/location
- Supports multiple Active Directory forests
- Dynamic OU placement based on configurable rules

### Intelligent Retry Logic
- Handles AD sync delays automatically
- Retries failed operations up to 3 times
- Self-healing capabilities for transient failures

### Access Replication
- Copies all security group memberships from template users
- Replicates Microsoft 365 groups and licenses
- Mirrors Atlassian/Jira project access

### Complete Audit Trail
- Every action logged in CloudWatch
- Real-time status updates in Jira tickets
- Full error tracking and notification system

## Setup Instructions

### Prerequisites
- AWS Account with appropriate permissions
- Active Directory with AWS Systems Manager access
- Microsoft 365 tenant with Graph API app registration
- Atlassian instance with API access

### Environment Variables
```bash
AD_CREDENTIALS_SECRET=your-ad-secret
JIRA_CREDENTIALS_SECRET=your-jira-secret
M365_CREDENTIALS_SECRET=your-m365-secret
M365_DELAY_QUEUE_URL=your-sqs-queue-url
ATLASSIAN=true
```

### Deployment
1. Deploy the CloudFormation template
2. Configure secrets in AWS Secrets Manager
3. Set up Jira automation rules
4. Configure cross-account IAM roles
5. Test with a sample employee

## Configuration Examples

### OU Mapping Configuration
```json
{
  "rules": [
    {
      "domain": "company.com",
      "ou": "OU=Users,DC=company,DC=com",
      "conditions": {
        "departments": ["IT", "Engineering"],
        "locations": ["HQ", "Remote"]
      }
    }
  ]
}
```

### Sample Event
```json
{
  "ticketKey": "ITSD-123",
  "employeeData": {
    "fullName": "John Doe",
    "firstName": "John",
    "lastName": "Doe",
    "department": "IT",
    "jobTitle": "Engineer",
    "copyAccessFrom": "jane.doe@company.com"
  }
}
```

## Security

- All credentials stored in AWS Secrets Manager
- Cross-account roles with minimal permissions
- Encrypted SNS/SQS messages
- No hardcoded secrets or credentials

## Performance Metrics

| Metric | Value |
|--------|-------|
| Average Execution Time | 45 seconds |
| Success Rate | 99.9% |
| Daily Capacity | 50+ employees |
| Cost per Onboarding | < $0.10 |

## Contributing

Feel free to submit issues and enhancement requests!

## License

This project is provided as-is for educational purposes.

## Author

**Amar Singh Thakur**
- LinkedIn: https://www.linkedin.com/in/amar-singh4352/
- GitHub: [@yourusername](https://github.com/yourusername)

---

*Note: This is a sanitized version of a production system. All sensitive information has been removed or replaced with placeholders.*
