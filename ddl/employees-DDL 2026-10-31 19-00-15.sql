-- jurong_marine.employees definition

CREATE TABLE "employees" (
  "emp_id" int NOT NULL,
  "emp_code" varchar(8) DEFAULT NULL,
  "emp_name" varchar(60) DEFAULT NULL,
  "trade" varchar(20) DEFAULT NULL,
  "shift" varchar(6) DEFAULT NULL,
  PRIMARY KEY ("emp_id")
);