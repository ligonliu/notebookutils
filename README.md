# notebookutils: pySpark notebook utility

generic notebookutils on arbitrary local/VM/K8s environment, for developing and running pySpark notebooks without Databricks / Azure Synapse / Microsoft Fabric environments.

## Introduction to **`dbutils`** and **`mssparkutils`** 
In cloud Platform-as-a-Service (PaaS) environments, **`dbutils`** and **`mssparkutils`** (recently rebranded as **`notebookutils`**) are essential, built-in utility packages designed to bridge the gap between PySpark code and the underlying cloud infrastructure.

While Apache Spark natively excels at distributed data processing, it lacks built-in mechanisms for environment-specific orchestration. These vendor-specific libraries step in to provide that critical operational control layer.

* **`dbutils` (Databricks Utilities):** The native toolset for the Databricks platform. It allows engineers to interactively navigate the Databricks File System (DBFS), securely retrieve credentials via Azure Key Vault or AWS Secrets Manager, parameterize notebooks using widgets, and chain multiple notebooks into modular workflows.
* **`mssparkutils` / `notebookutils` (Microsoft Spark Utilities):** The direct counterpart built for Microsoft Azure Synapse Analytics and Microsoft Fabric. It offers near-identical functionality and APIs tailored for the Microsoft ecosystem, enabling developers to manage files in Azure Data Lake Storage (ADLS) Gen2, handle Microsoft Entra ID tokens, and control notebook pipeline execution paths.

Ultimately, both packages abstract away complex cloud APIs, allowing data engineers to write secure, scalable, and maintainable data pipelines without leaving their interactive PySpark notebook environments.

## Installation

Install `notebookutils`

```sh
pip install notebookutils
```

## Usage

### Configuration Files

notebookutils read the following configuration files for authenticating to cloud resources. They need to be placed before accessing cloud file systems and secrets:


.notebookutils/storage/account_name.azure.yaml
.notebookutils/identity.yaml

### 
Add the following code below your other imports:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import notebookutils

    from notebookutils import mssparkutils
```
