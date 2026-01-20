import os

from pyspark.sql import SparkSession
from pyspark.sql.functions import rand, round as spark_round

start_date = os.getenv("START_DATE", "2025-01-01")
end_date = os.getenv("END_DATE", "2025-12-31")
num_accounts = int(os.getenv("NUM_ACCOUNTS", "12"))

catalog = os.getenv("UC_CATALOG", "main")
schema = os.getenv("UC_SCHEMA", "variance_analysis")
accounts_table = os.getenv("UC_ACCOUNTS_TABLE", "accounts")
sales_table = os.getenv("UC_SALES_TABLE", "sales")

spark = SparkSession.builder.appName("synthetic-sales").getOrCreate()

spark.sql(f"CREATE CATALOG IF NOT EXISTS {catalog}")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")

accounts = [chr(65 + i) for i in range(num_accounts)]
account_df = spark.createDataFrame([(a,) for a in accounts], ["account_name"])

date_df = spark.sql(
    f"SELECT explode(sequence(to_date('{start_date}'), to_date('{end_date}'), interval 1 day)) AS sale_date"
)

sales_df = account_df.crossJoin(date_df)

sales_df = sales_df.withColumn(
    "amount",
    spark_round((rand() * 2000) + (rand() * 3000), 2),
)

account_df.write.mode("overwrite").saveAsTable(f"{catalog}.{schema}.{accounts_table}")

account_ids = spark.table(f"{catalog}.{schema}.{accounts_table}")

sales_with_ids = sales_df.join(account_ids, "account_name") \
    .select("account_name", "sale_date", "amount")

sales_with_ids.write.mode("overwrite").saveAsTable(f"{catalog}.{schema}.{sales_table}")

spark.stop()
