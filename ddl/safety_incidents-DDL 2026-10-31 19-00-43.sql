-- jurong_marine.safety_incidents definition

CREATE TABLE "safety_incidents" (
  "incident_id" int NOT NULL,
  "incident_date" date DEFAULT NULL,
  "machine_id" varchar(10) DEFAULT NULL,
  "description" varchar(90) DEFAULT NULL,
  "severity" int DEFAULT NULL,
  "lost_days" int DEFAULT NULL,
  "emp_code" varchar(8) DEFAULT NULL,
  "reporting" varchar(30) DEFAULT NULL,
  PRIMARY KEY ("incident_id")
);