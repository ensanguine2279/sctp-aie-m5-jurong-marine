-- jurong_marine.work_orders definition

CREATE TABLE "work_orders" (
  "wo_id" int NOT NULL,
  "machine_id" varchar(10) DEFAULT NULL,
  "wo_type" varchar(12) DEFAULT NULL,
  "description" varchar(80) DEFAULT NULL,
  "wo_date" date DEFAULT NULL,
  "duration_hrs" decimal(5,1) DEFAULT NULL,
  "cost_sgd" decimal(8,2) DEFAULT NULL,
  "emp_code" varchar(8) DEFAULT NULL,
  "status" varchar(16) DEFAULT NULL,
  PRIMARY KEY ("wo_id")
);