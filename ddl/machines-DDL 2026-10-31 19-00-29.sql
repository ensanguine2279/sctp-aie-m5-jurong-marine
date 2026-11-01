-- jurong_marine.machines definition

CREATE TABLE "machines" (
  "machine_id" varchar(10) NOT NULL,
  "machine_name" varchar(40) DEFAULT NULL,
  "model" varchar(40) DEFAULT NULL,
  "year_installed" int DEFAULT NULL,
  PRIMARY KEY ("machine_id")
);