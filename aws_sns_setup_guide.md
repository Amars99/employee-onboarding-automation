# AWS SNS & Lambda Setup Guide

## What This Creates

This setup creates the AWS infrastructure that receives messages from Jira and processes employee onboarding:

1. **SNS Topic** - Receives messages from Jira
2. **Lambda Function** - Processes the onboarding (your Python code)
3. **SQS Queue** - Handles delayed M365/Atlassian processing
4. **IAM Roles** - Permissions for everything to work together

## Step 1: Prepare Your AWS Account

### Required Services
- AWS Lambda
- Amazon SNS
- Amazon SQS  
- AWS Secrets Manager
- AWS Systems Manager (SSM)
- AWS IAM

### Create Secrets First
Before deploying, create these secrets in AWS Secrets Manager:

1. **AD Credentials** (`employee-onboarding-prod-ad-credentials`):
```json
{
  "username": "DOMAIN\\admin-user",
  "password": "your-password"
}
```

2. **Jira Credentials** (`employee-onboarding-prod-jira-credentials`):
```json
{
  "username": "your-email@company.com",
  "apiToken": "your-jira-api-token"
}
```

3. **Microsoft 365** (`employee-onboarding-prod-m365-credentials`):
```json
{
  "tenant_id": "your-tenant-id",
  "client_id": "your-app-id",
  "client_secret": "your-app-secret"
}
```

4. **OU Mapping** (`employee-onboarding-prod-ou-mapping`):
```json
{
  "rules": [
    {
      "domain": "company.com",
      "ou": "OU=Users,DC=company,DC=com",
      "conditions": {
        "departments": ["IT", "Engineering"]
      }
    }
  ],
  "default": {
    "domain": "company.com",
    "ou": "OU=Users,DC=company,DC=com"
  }
}
```

## Step 2: Deploy Using CloudFormation

### Option A: Use the Simple Jira Template (Basic)
This creates ONLY the Jira-to-SNS connection:

```yaml
# Save as: jira-sns-basic.yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: 'Basic SNS topic for Jira Automation'

Parameters:
  TopicName:
    Type: String
    Default: employee-onboarding-prod-trigger

Resources:
  JiraTopic:
    Type: AWS::SNS::Topic
    Properties:
      TopicName: !Ref TopicName
      DisplayName: Employee Onboarding Trigger
      
  JiraTopicPolicy:
    Type: AWS::SNS::TopicPolicy
    Properties:
      Topics:
        - !Ref JiraTopic
      PolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Sid: 'AllowAtlassianPublish'
            Effect: Allow
            Principal:
              AWS: '815843069303'  # Atlassian's AWS account
            Action: 'sns:Publish'
            Resource: !Ref JiraTopic

Outputs:
  TopicArn:
    Description: Use this ARN in Jira Automation
    Value: !Ref JiraTopic
```

### Option B: Use the Complete Template (Recommended)
Use the comprehensive template I provided earlier that includes Lambda, SQS, and all connections.

### Deploy via Console
1. Go to **AWS CloudFormation**
2. Click **Create stack** → **With new resources**
3. **Upload template file** → Choose your YAML file
4. **Stack name**: `employee-onboarding-prod`
5. **Parameters**: Keep defaults or customize
6. **Next** → **Next** → Check "I acknowledge IAM resources"
7. **Create stack**
8. Wait for **CREATE_COMPLETE** (3-5 minutes)
9. Go to **Outputs** tab → Copy the **TopicArn**

## Step 3: Upload Your Lambda Code

### After Stack Creation
1. Go to **AWS Lambda** console
2. Find function: `employee-onboarding-prod-processor`
3. Click on the function name
4. In **Code source**, replace with your sanitized Python code
5. Click **Deploy**

### Configure Environment Variables
1. Go to **Configuration** → **Environment variables**
2. Add these variables:

| Key | Value |
|-----|-------|
| AD_CREDENTIALS_SECRET | employee-onboarding-prod-ad-credentials |
| JIRA_CREDENTIALS_SECRET | employee-onboarding-prod-jira-credentials |
| M365_CREDENTIALS_SECRET | employee-onboarding-prod-m365-credentials |
| OU_MAPPING_SECRET | employee-onboarding-prod-ou-mapping |
| M365_DELAY_QUEUE_URL | (copy from CloudFormation Outputs) |
| ATLASSIAN | true |
| PROD_ACCOUNT_ID | your-production-account-id |
| JIRA_URL | https://your-company.atlassian.net |
| EMAIL_FORMAT | firstname.lastname |

## Step 4: Set Up Cross-Account Access (If Needed)

If your Domain Controllers are in a different AWS account:

### In Production Account (with DCs)
1. Create IAM Role: `EmployeeOnboardingCrossAccountSSMRole`
2. Trust policy:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::DEV-ACCOUNT-ID:root"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "employee-onboarding-prod-access"
        }
      }
    }
  ]
}
```

3. Attach policy allowing SSM commands:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ssm:SendCommand",
        "ssm:GetCommandInvocation",
        "ec2:DescribeInstances"
      ],
      "Resource": "*"
    }
  ]
}
```

## Step 5: Test the Setup

### Test via Jira
1. Create test ticket with employee data
2. Transition to "In Progress"
3. Check CloudWatch logs for Lambda

### Test Lambda Directly
1. Go to Lambda console
2. Click **Test** tab
3. Create test event:
```json
{
  "Records": [
    {
      "Sns": {
        "Message": "{\"ticketKey\":\"TEST-001\",\"employeeData\":{\"fullName\":\"Test User\",\"firstName\":\"Test\",\"lastName\":\"User\",\"department\":\"IT\"}}"
      }
    }
  ]
}
```
4. Click **Test**
5. Check execution results

## Step 6: Monitor & Troubleshoot

### CloudWatch Logs
- Location: `/aws/lambda/employee-onboarding-prod-processor`
- Look for: Error messages, execution time, success confirmations

### Common Issues & Solutions

| Issue | Solution |
|-------|----------|
| SNS permission denied | Verify Atlassian account ID (815843069303) in topic policy |
| Lambda timeout | Increase timeout to 300 seconds |
| Secrets not found | Check secret names match exactly |
| SQS messages not processing | Verify SQS event source mapping is enabled |
| Cross-account access denied | Check role trust policy and external ID |

### Monitoring Dashboard
Create CloudWatch dashboard with:
- Lambda invocations
- Lambda errors
- Lambda duration
- SQS message age
- SNS publish success rate

## Architecture Flow

```
1. Jira Ticket Status Change
       ↓
2. Jira Automation Rule Triggers
       ↓
3. Sends Message to SNS Topic
       ↓
4. SNS Triggers Lambda Function
       ↓
5. Lambda Creates AD Account
       ↓
6. Lambda Schedules SQS Message (15 min delay)
       ↓
7. SQS Triggers Lambda Again
       ↓
8. Lambda Assigns M365 License & Creates Atlassian Account
       ↓
9. Updates Jira Ticket with Results
```

## Security Best Practices

1. **Never hardcode credentials** - Use Secrets Manager
2. **Use least privilege IAM** - Only grant necessary permissions
3. **Enable encryption** - Use KMS for SNS/SQS if handling sensitive data
4. **Monitor access** - Set up CloudTrail for audit logs
5. **Rotate secrets regularly** - Update API tokens periodically
6. **Use external IDs** - For cross-account role assumptions
7. **Restrict SNS topic** - Only allow Atlassian's specific account

## Cost Optimization

- Lambda: ~$0.10 per 1000 executions
- SNS: First 1M requests free
- SQS: First 1M requests free
- Secrets Manager: $0.40 per secret per month
- CloudWatch Logs: $0.50 per GB ingested

**Estimated monthly cost**: < $10 for 500 onboardings
